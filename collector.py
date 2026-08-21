# -*- coding: utf-8 -*-
"""行业情报雷达 - 多源采集器
数据源优先级：
1. 重点厂商官网新闻/方案页（权威、直达原文、反爬低）
2. 百度资讯搜索（国内新闻聚合）
3. 必应中国搜索（备用）
4. 搜狗网页搜索（最后兜底）
已移除：搜狗微信搜索（腾讯云机房 IP 触发验证码，原文需验证）
"""
import re
import time
import html as html_mod
import random
import threading
from urllib.parse import quote, urljoin, urlparse

import requests
from bs4 import BeautifulSoup  # 使用标准库 html.parser，无需 lxml

import config
import database

HEADERS = {
    'User-Agent': ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
                   '(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'),
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
    'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.5',
    'Referer': 'https://www.baidu.com/',
}


def _clean_text(s):
    """清洗 HTML 标签、空白和乱码"""
    if not s:
        return ''
    s = html_mod.unescape(s)
    s = re.sub(r'<[^>]+>', ' ', s)
    s = re.sub(r'[\s\u200b\u200c\u200d\xa0]+', ' ', s).strip()
    return s


def _random_ua():
    """生成随机桌面 User-Agent，降低被风控概率"""
    versions = [
        'Chrome/118.0.0.0 Safari/537.36',
        'Chrome/119.0.0.0 Safari/537.36',
        'Chrome/120.0.0.0 Safari/537.36',
        'Chrome/121.0.0.0 Safari/537.36',
        'Edg/120.0.0.0 Safari/537.36',
    ]
    return 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) ' + random.choice(versions)


def _request_get(url, timeout=10, headers=None):
    """通用 GET 请求，带随机 UA 和异常处理"""
    h = dict(HEADERS)
    h['User-Agent'] = _random_ua()
    if headers:
        h.update(headers)
    try:
        resp = requests.get(url, headers=h, timeout=timeout, allow_redirects=True)
        resp.raise_for_status()
        # 尝试多种编码
        resp.encoding = resp.apparent_encoding or 'utf-8'
        return resp
    except Exception as e:
        database.log('collect', '请求失败: {} | {}'.format(url[:80], e), 'warn')
        return None


def _is_antispider(text):
    """检测是否被反爬"""
    if not text:
        return True
    signs = ['请输入验证码', '安全验证', 'seccodeInput', 'antispider', '验证码', '访问过于频繁']
    return any(s in text for s in signs)


def _extract_date(text):
    """从文本中提取常见日期格式"""
    if not text:
        return ''
    patterns = [
        r'(\d{4}-\d{1,2}-\d{1,2})',
        r'(\d{4}年\d{1,2}月\d{1,2}日)',
        r'(\d{4}/\d{1,2}/\d{1,2})',
        r'(\d{1,2}-\d{1,2})',
    ]
    for p in patterns:
        m = re.search(p, text)
        if m:
            return m.group(1)
    return ''


# ==================== 搜索引擎采集 ====================

def fetch_baidu_news(query, max_results=8):
    """百度资讯搜索：返回 [{title,url,source,summary,published}]"""
    url = 'https://news.baidu.com/ns?word={}&tn=news&from=news&cl=2&rn=20&ct=0'.format(quote(query))
    items = []
    resp = _request_get(url, timeout=12)
    if not resp:
        return items
    text = resp.text
    if _is_antispider(text):
        database.log('collect', '百度资讯触发验证: {}'.format(query), 'warn')
        return items

    soup = BeautifulSoup(text, 'html.parser')
    # 过滤低质量域名
    LOW_QUALITY_DOMAINS = ['zhidao.baidu.com', 'tieba.baidu.com', 'baike.baidu.com',
                           'wenku.baidu.com', 'zhihu.com/question']
    # 百度新闻结果通常在 div.result 或 div.c-container
    containers = soup.select('div.result') or soup.select('div.c-container')
    for c in containers[:max_results]:
        a = c.select_one('h3 a')
        if not a:
            a = c.find('a')
        if not a:
            continue
        title = _clean_text(a.get_text())
        link = a.get('href', '')
        if not title or not link:
            continue
        # 过滤低质量域名
        if any(d in link for d in LOW_QUALITY_DOMAINS):
            continue
        # 百度新闻 href 有时是跳转链接，尝试解析真实链接
        if link.startswith('http://news.baidu.com/n'):
            real = _unescape_baidu_link(link)
            if real:
                link = real
        summary_el = c.select_one('span.content-right_8ZsCE, div.c-span9, p')
        summary = _clean_text(summary_el.get_text() if summary_el else '')
        source_el = c.select_one('span.c-color-gray, p.c-author')
        source = _clean_text(source_el.get_text() if source_el else '')
        published = _extract_date(source)
        # 来源里通常包含日期，清洗一下
        if source:
            source = re.sub(r'\d{4}[-/]\d{1,2}[-/]\d{1,2}.*$', '', source)
            source = re.sub(r'\d{1,2}:\d{2}.*$', '', source).strip()
        if not source:
            source = '百度资讯'
        items.append({
            'title': title,
            'url': link,
            'source': source,
            'published': published,
            'summary': summary,
        })
    return items


def _unescape_baidu_link(jump_url):
    """尝试解析百度跳转链接中的真实 URL（简单解析）"""
    try:
        resp = _request_get(jump_url, timeout=8, headers={'Referer': 'https://news.baidu.com/'})
        if resp and resp.url and not resp.url.startswith('http://news.baidu.com'):
            return resp.url
    except Exception:
        pass
    return ''


def fetch_bing(query, max_results=8):
    """必应中国搜索：返回 [{title,url,source,summary,published}]"""
    url = 'https://cn.bing.com/search?q={}&setmkt=zh-CN&setlang=zh-CN&FORM=BEHPTB'.format(quote(query))
    items = []
    resp = _request_get(url, timeout=12, headers={'Referer': 'https://cn.bing.com/'})
    if not resp:
        return items
    text = resp.text
    if _is_antispider(text):
        database.log('collect', '必应触发验证: {}'.format(query), 'warn')
        return items

    soup = BeautifulSoup(text, 'html.parser')
    NOISE_DOMAINS = ['baike.baidu.com', 'wikipedia.org', 'zhihu.com/question',
                     'www.zhihu.com', 'tieba.baidu.com']
    for li in soup.select('li.b_algo')[:max_results * 2]:
        a = li.select_one('h2 a')
        if not a:
            continue
        title = _clean_text(a.get_text())
        link = a.get('href', '')
        if not title or not link:
            continue
        # 过滤百科/问答/首页
        if any(nd in link for nd in NOISE_DOMAINS):
            continue
        parsed = urlparse(link)
        if parsed.path in ('', '/'):
            continue
        summary_el = li.select_one('p')
        summary = _clean_text(summary_el.get_text() if summary_el else '')
        source_el = li.select_one('cite, div.b_attribution')
        source = _clean_text(source_el.get_text() if source_el else '')
        if not source:
            source = '必应'
        items.append({
            'title': title,
            'url': link,
            'source': source,
            'published': '',
            'summary': summary,
        })
        if len(items) >= max_results:
            break
    return items


def fetch_sogou_web(query, max_results=6):
    """搜狗网页搜索（兜底）：返回 [{title,url,source,summary,published}]"""
    url = 'https://www.sogou.com/web?query={}&page=1'.format(quote(query))
    items = []
    resp = _request_get(url, timeout=10)
    if not resp:
        return items
    text = resp.text
    if _is_antispider(text):
        database.log('collect', '搜狗网页触发验证: {}'.format(query), 'warn')
        return items

    # 搜狗结果一般在 h3 a
    results = re.findall(r'<h3[^>]*>\s*<a[^>]*href="([^"]+)"[^>]*>(.*?)</a>', text, re.S)
    for link, title in results[:max_results]:
        title = _clean_text(title)
        if not title or not link:
            continue
        if not link.startswith('http'):
            link = urljoin('https://www.sogou.com', link)
        items.append({'title': title, 'url': link, 'source': '搜狗',
                      'published': '', 'summary': ''})
    return items


# ==================== 厂商官网采集 ====================

def fetch_official_page(vendor_name, page_url, keywords, max_results=5):
    """
    通用官网页面解析：抓取 page_url，遍历所有 a 标签，
    标题包含任一 keyword 的视为该厂商新闻/方案。
    返回 [{title,url,source,summary,published}]
    """
    items = []
    if not page_url:
        return items
    resp = _request_get(page_url, timeout=12)
    if not resp:
        return items
    text = resp.text
    if _is_antispider(text):
        database.log('collect', '官网触发验证: {} {}'.format(vendor_name, page_url), 'warn')
        return items

    base_url = '{}://{}'.format(urlparse(resp.url).scheme, urlparse(resp.url).netloc)
    soup = BeautifulSoup(text, 'html.parser')
    seen = set()

    # 常见无意义链接文本
    NOISE_WORDS = ['查看更多', '阅读更多', '更多', 'more', '详情', '点击了解',
                   '了解详情', '立即咨询', '在线咨询', '免费试用', '首页', '返回']

    # 内容页路径特征
    CONTENT_PATHS = ['news', 'article', 'detail', 'solutions', 'solution',
                     'case', 'cases', 'product', 'products', 'about', 'intro']

    for a in soup.find_all('a', href=True):
        title = _clean_text(a.get_text())
        if not title or len(title) < 10 or len(title) > 80:
            continue
        # 过滤无意义导航链接
        if any(w in title for w in NOISE_WORDS):
            continue
        href = a['href'].strip()
        if href.startswith('#') or href.startswith('javascript:'):
            continue
        link = urljoin(resp.url, href)
        parsed = urlparse(link)
        path_lower = parsed.path.lower()
        # 过滤首页/根路径
        if parsed.path in ('', '/', '/index.html', '/index.htm', '/index.php'):
            continue
        # 命中关键词 或 URL 明显是内容页
        title_hit = any(kw in title for kw in keywords) or any(kw in a.get('title', '') for kw in keywords)
        path_hit = any('/' + cp in path_lower or path_lower.endswith('/' + cp) for cp in CONTENT_PATHS)
        if not (title_hit or path_hit):
            continue
        if link in seen:
            continue
        seen.add(link)

        # 摘要：尝试找同一段落或附近 div 的文本
        summary = ''
        parent = a.find_parent(['div', 'li', 'p'])
        if parent:
            summary = _clean_text(parent.get_text())
            # 去掉标题本身
            summary = summary.replace(title, '').strip()
        published = _extract_date(str(parent) + str(a))

        items.append({
            'title': title,
            'url': link,
            'source': vendor_name,
            'published': published,
            'summary': summary[:200],
        })
        if len(items) >= max_results:
            break

    return items


# 重点厂商官网配置（URL 和关键词）
# 为空/失败时自动降级到搜索引擎
OFFICIAL_PAGES = [
    {'vendor': '海康威视', 'url': 'https://www.hikvision.com/cn/newsCenter/', 'keywords': ['数字孪生', '视频融合', '三维', '智慧', '发布', '方案']},
    {'vendor': '海康威视', 'url': 'https://www.hikvision.com/cn/solutions/', 'keywords': ['数字孪生', '视频融合', '三维', '智慧', '园区', '建筑']},
    {'vendor': '51WORLD', 'url': 'http://www.51world.com.cn/news.html', 'keywords': ['数字孪生', '三维', '发布', '方案', '智慧']},
    {'vendor': '大华股份', 'url': 'https://www.dahuatech.com/news/', 'keywords': ['数字孪生', '视频融合', '三维', '智慧', '发布', '方案']},
    {'vendor': '华为', 'url': 'https://e.huawei.com/cn/news', 'keywords': ['数字孪生', '三维', '智慧园区', '智慧建筑', '发布']},
    {'vendor': '超图软件', 'url': 'http://www.supermap.com.cn/news/', 'keywords': ['数字孪生', '三维GIS', 'GIS', '发布', '方案']},
    {'vendor': '数字冰雹', 'url': 'http://www.digihail.com/news/', 'keywords': ['数字孪生', '三维', '可视化', '发布', '方案']},
    {'vendor': '优锘科技', 'url': 'https://www.uino.com/news', 'keywords': ['数字孪生', '三维', '可视化', '发布', '方案']},
    {'vendor': '智汇云舟', 'url': 'http://www.biosphere3.com/news', 'keywords': ['数字孪生', '视频融合', '三维', '发布', '方案']},
]


def fetch_vendor_official(vendor_name, max_results=5):
    """采集指定厂商的所有官网页面"""
    items = []
    pages = [p for p in OFFICIAL_PAGES if p['vendor'] == vendor_name]
    if not pages:
        # 没有配置官网时，尝试用厂商名+常见新闻页猜测
        return items
    for p in pages:
        page_items = fetch_official_page(vendor_name, p['url'], p['keywords'], max_results)
        items.extend(page_items)
        time.sleep(0.8)
    # 去重
    seen = set()
    unique = []
    for it in items:
        if it['url'] in seen:
            continue
        seen.add(it['url'])
        unique.append(it)
    return unique[:max_results]


# ==================== 聚合采集逻辑 ====================

def _dedup_by_url(items):
    """按 URL 去重，保留先出现的"""
    seen = set()
    out = []
    for it in items:
        url = it.get('url', '')
        if not url or url in seen:
            continue
        seen.add(url)
        out.append(it)
    return out


def fetch_query(query, vendor_name='', max_results=8):
    """
    综合采集一个查询：
    1. 优先该厂商官网（如果配置）
    2. 百度资讯
    3. 必应中国
    4. 搜狗网页兜底
    返回去重后的结果列表
    """
    all_items = []

    # 1. 官网
    if vendor_name:
        official = fetch_vendor_official(vendor_name, max_results=max_results)
        all_items.extend(official)
        time.sleep(0.5)

    # 2. 百度资讯
    baidu = fetch_baidu_news(query, max_results=max_results)
    all_items.extend(baidu)
    time.sleep(1.0)

    # 3. 必应
    bing = fetch_bing(query, max_results=max_results)
    all_items.extend(bing)
    time.sleep(1.0)

    # 4. 搜狗兜底
    sogou = fetch_sogou_web(query, max_results=max_results)
    all_items.extend(sogou)

    # 去重：同 URL 只留一个；优先保留有来源的
    seen = {}
    for it in all_items:
        url = it.get('url', '')
        if not url:
            continue
        if url not in seen or (it.get('source') and not seen[url].get('source')):
            seen[url] = it

    results = list(seen.values())[:max_results]
    # 补一个来源标记
    for r in results:
        if not r.get('source'):
            r['source'] = '网络'
    return results


# ==================== 评分与标签（与之前保持一致） ====================

def score_relevance(title, desc):
    """按关键词匹配给相关度打分 1-5"""
    text = (title + ' ' + desc).lower()
    score = 0
    for kw in config.RELEVANCE_HIGH:
        if kw.lower() in text:
            score += 2
    for kw in config.RELEVANCE_MEDIUM:
        if kw.lower() in text:
            score += 1
    for kw in config.RELEVANCE_LOW:
        if kw.lower() in text:
            score += 0.5
    score = int(round(score))
    return max(1, min(5, score))


def detect_tags(title, desc):
    """按关键词打标签"""
    text = (title + ' ' + desc).lower()
    tags = []
    if any(k in text for k in ['中标', '招标', '采购', '签约', '项目', '落地']):
        tags.append('案例')
    if any(k in text for k in ['发布', '推出', '新品', '升级', '版本', '专利', '研发']):
        tags.append('技术')
    if any(k in text for k in ['政策', '标准', '意见', '规划', '通知', '白皮书', '指导意见']):
        tags.append('政策')
    if any(k in text for k in ['合作', '战略', '生态', '伙伴', '携手']):
        tags.append('方案')
    if any(k in text for k in ['展会', '大会', '论坛', '峰会', '博览会', 'ebc']):
        tags.append('展会')
    if not tags:
        tags.append('方案')
    return tags[:2]


def detect_industry(title, desc):
    """判断所属行业"""
    text = title + ' ' + desc
    for ind in config.INDUSTRIES:
        if ind in text:
            return ind
    return ''


# ==================== 异步进度与主采集 ====================

PROGRESS = {
    'running': False,
    'total': 0,
    'done': 0,
    'added': 0,
    'errors': 0,
    'current': '',
    'finished_at': '',
}
_lock = threading.Lock()


def collect_status():
    """返回当前采集进度（副本）"""
    with _lock:
        return dict(PROGRESS)


def _update_progress(running=None, total=None, done=None, added=None,
                     errors=None, current=None, finished_at=None):
    with _lock:
        if running is not None:
            PROGRESS['running'] = running
        if total is not None:
            PROGRESS['total'] = total
        if done is not None:
            PROGRESS['done'] = done
        if added is not None:
            PROGRESS['added'] = added
        if errors is not None:
            PROGRESS['errors'] = errors
        if current is not None:
            PROGRESS['current'] = current
        if finished_at is not None:
            PROGRESS['finished_at'] = finished_at


def collect_once():
    """执行一次完整采集，返回新增条数。每天晚上单线程低频率运行。"""
    added = 0
    errors = 0
    queries = []

    for vendor in config.VENDORS:
        for kw in vendor['keywords']:
            queries.append((vendor['name'], kw, vendor['name']))
    for q in config.INDUSTRY_QUERIES:
        queries.append(('', q, ''))

    _update_progress(running=True, total=len(queries), done=0, added=0,
                     errors=0, current='', finished_at='')

    # 全量去重：避免同一新闻被不同厂商关键词重复抓
    url_seen = set()
    title_seen = set()

    for i, (vendor, query, vendor_name) in enumerate(queries):
        _update_progress(done=i, current=query)
        try:
            items = fetch_query(query, vendor_name=vendor_name, max_results=config.MAX_PER_QUERY)
            for it in items:
                title = it.get('title', '').strip()
                url = it.get('url', '').strip()
                if not title or not url:
                    continue
                # 全局去重
                if url in url_seen or title in title_seen:
                    continue
                url_seen.add(url)
                title_seen.add(title)

                industry = detect_industry(title, it.get('summary', ''))
                rel = score_relevance(title, it.get('summary', ''))
                tags = detect_tags(title, it.get('summary', ''))
                ok = database.add_intelligence({
                    'date': database.today_str(),
                    'vendor': vendor_name,
                    'industry': industry,
                    'title': title,
                    'source': it.get('source', ''),
                    'url': url,
                    'summary': (it.get('summary') or '')[:200],
                    'description': it.get('summary', ''),
                    'relevance': rel,
                    'tags': tags,
                })
                if ok:
                    added += 1
        except Exception as e:
            errors += 1
            database.log('collect', '查询失败: {} | {}'.format(query, e), 'warn')
        # 夜间低频率：1.5~2.5 秒随机间隔，降低被封概率
        time.sleep(random.uniform(1.5, 2.5))

    _update_progress(running=False, done=len(queries), added=added,
                     errors=errors, current='', finished_at=database.now_str())
    database.log('collect', '新增 {} 条 / 查询 {} 组 / 错误 {}'.format(
        added, len(queries), errors), 'ok' if errors == 0 else 'warn')
    return added


if __name__ == '__main__':
    database.init_db()
    n = collect_once()
    print('本次采集新增 {} 条'.format(n))
