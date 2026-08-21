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

# 更完整的浏览器请求头，模拟真实 Chrome
BASE_HEADERS = {
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8',
    'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8,en-GB;q=0.7,en-US;q=0.6',
    'Accept-Encoding': 'gzip, deflate, br',
    'DNT': '1',
    'Connection': 'keep-alive',
    'Upgrade-Insecure-Requests': '1',
    'Sec-Fetch-Dest': 'document',
    'Sec-Fetch-Mode': 'navigate',
    'Sec-Fetch-Site': 'none',
    'Sec-Fetch-User': '?1',
    'Cache-Control': 'max-age=0',
}

# 全局 session：复用连接、自动维护 cookie
_session = requests.Session()
_session.headers.update(BASE_HEADERS)


def _random_ua():
    """生成随机桌面 User-Agent，降低被风控概率"""
    versions = [
        ('Chrome/118.0.0.0 Safari/537.36', '"Chromium";v="118", "Google Chrome";v="118", "Not=A?Brand";v="24"'),
        ('Chrome/119.0.0.0 Safari/537.36', '"Chromium";v="119", "Google Chrome";v="119", "Not=A?Brand";v="24"'),
        ('Chrome/120.0.0.0 Safari/537.36', '"Chromium";v="120", "Google Chrome";v="120", "Not=A?Brand";v="99"'),
        ('Chrome/121.0.0.0 Safari/537.36', '"Chromium";v="121", "Google Chrome";v="121", "Not=A?Brand";v="99"'),
        ('Edg/120.0.0.0 Safari/537.36', '"Chromium";v="120", "Microsoft Edge";v="120", "Not=A?Brand";v="99"'),
    ]
    ver, sec = random.choice(versions)
    return {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) ' + ver,
        'Sec-Ch-Ua': sec,
        'Sec-Ch-Ua-Mobile': '?0',
        'Sec-Ch-Ua-Platform': '"Windows"',
    }


def _request_get(url, timeout=10, headers=None, referer=None):
    """通用 GET 请求，带随机 UA、session cookie 和异常处理"""
    h = _random_ua()
    h['Referer'] = referer or 'https://www.baidu.com/'
    if headers:
        h.update(headers)
    try:
        resp = _session.get(url, headers=h, timeout=timeout, allow_redirects=True)
        resp.raise_for_status()
        resp.encoding = resp.apparent_encoding or 'utf-8'
        return resp
    except Exception as e:
        database.log('collect', '请求失败: {} | {}'.format(url[:80], e), 'warn')
        return None


def _clean_text(s):
    """清洗 HTML 标签、空白和乱码"""
    if not s:
        return ''
    s = html_mod.unescape(s)
    s = re.sub(r'<[^>]+>', ' ', s)
    s = re.sub(r'[\s\u200b\u200c\u200d\xa0]+', ' ', s).strip()
    return s


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
    items = []
    # 先预热百度首页，获取 cookie，降低被反爬概率
    _request_get('https://www.baidu.com/', timeout=8, referer='https://www.baidu.com/')
    time.sleep(random.uniform(0.3, 0.8))

    # 使用百度主站资讯搜索（结构更稳定）
    url = 'https://www.baidu.com/s?rtt=1&bsst=1&cl=2&tn=news&word={}&ie=utf-8'.format(quote(query))
    resp = _request_get(url, timeout=12, referer='https://www.baidu.com/')
    if not resp:
        return items
    text = resp.text
    if _is_antispider(text):
        database.log('collect', '百度资讯触发验证: {}'.format(query), 'warn')
        return items

    soup = BeautifulSoup(text, 'html.parser')
    LOW_QUALITY_DOMAINS = ['zhidao.baidu.com', 'tieba.baidu.com', 'baike.baidu.com',
                           'wenku.baidu.com', 'zhihu.com/question', 'jingyan.baidu.com']

    # 新版百度资讯结果结构多样：div.result / div.c-container / div[data-module]
    containers = soup.select('div.result, div.c-container, div.c-row, div.ops-line, div.news-item')
    for c in containers[:max_results * 2]:
        a = c.select_one('h3 a')
        if not a:
            a = c.find('a')
        if not a:
            continue
        title = _clean_text(a.get_text())
        link = a.get('href', '')
        if not title or not link or len(title) < 8:
            continue
        if any(d in link for d in LOW_QUALITY_DOMAINS):
            continue
        if link.startswith('http://news.baidu.com/n') or link.startswith('https://www.baidu.com/link'):
            real = _unescape_baidu_link(link)
            if real:
                link = real
            else:
                continue
        summary_el = c.select_one('span.content-right_8ZsCE, div.c-span9, span.c-color-text, p, div.content-right_8ZsCE')
        summary = _clean_text(summary_el.get_text() if summary_el else '')
        source_el = c.select_one('span.c-color-gray, p.c-author, div.c-color-gray, a.c-font-medium')
        source = _clean_text(source_el.get_text() if source_el else '')
        published = _extract_date(source)
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
        if len(items) >= max_results:
            break
    database.log('collect', '百度资讯[{}] 原始结果{}条 有效{}条'.format(query, len(containers), len(items)), 'ok')
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
    resp = _request_get(url, timeout=12, referer='https://cn.bing.com/')
    if not resp:
        return items
    text = resp.text
    if _is_antispider(text):
        database.log('collect', '必应触发验证: {}'.format(query), 'warn')
        return items

    soup = BeautifulSoup(text, 'html.parser')
    NOISE_DOMAINS = ['baike.baidu.com', 'wikipedia.org', 'zhihu.com/question',
                     'www.zhihu.com', 'tieba.baidu.com', 'quote.eastmoney.com',
                     'download.', 'ws.com.cn', 'products']
    # 必应新版结果结构：li.b_algo 或 div.b_algo 或 div[data-idx]
    candidates = soup.select('li.b_algo, div.b_algo, div.b_title')
    skipped = 0
    for li in candidates[:max_results * 3]:
        a = li.select_one('h2 a') or li.select_one('a')
        if not a:
            continue
        title = _clean_text(a.get_text())
        link = a.get('href', '')
        if not title or not link or len(title) < 10:
            skipped += 1
            continue
        if any(nd in link for nd in NOISE_DOMAINS):
            skipped += 1
            continue
        parsed = urlparse(link)
        if parsed.path in ('', '/', '/index.html'):
            skipped += 1
            continue
        # 过滤明显是频道首页/下载页的链接
        path_lower = parsed.path.lower()
        if any(path_lower.endswith('/' + x) for x in ['products', 'product', 'download', 'downloads']):
            skipped += 1
            continue
        # 保留路径较深或标题明显相关的页面
        if path_lower.count('/') <= 1 and not any(k in title for k in config.RELEVANCE_HIGH):
            skipped += 1
            continue
        summary_el = li.select_one('p, div.b_caption')
        summary = _clean_text(summary_el.get_text() if summary_el else '')
        source_el = li.select_one('cite, div.b_attribution, span[dir="ltr"]')
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
    database.log('collect', '必应[{}] 原始结果{}条 跳过{}条 有效{}条'.format(query, len(candidates), skipped, len(items)), 'ok')
    return items


def _resolve_sogou_link(link):
    """访问搜狗跳转链接，解析出真实 URL"""
    if not link.startswith('https://www.sogou.com/link'):
        return link
    try:
        resp = _request_get(link, timeout=8, referer='https://www.sogou.com/')
        if not resp:
            return link
        text = resp.text
        # window.location.replace("http://...")
        m = re.search(r'window\.location\.replace\(["\']([^"\']+)["\']\)', text)
        if m:
            return m.group(1)
        # <META http-equiv="refresh" content="0;URL='...'">
        m = re.search(r'<meta[^>]+http-equiv=["\']refresh["\'][^>]+url=["\']?([^"\'>\s]+)', text, re.I)
        if m:
            return m.group(1).strip("'\"")
        return resp.url if resp.url != link else link
    except Exception:
        return link


def _sogou_home_warmup():
    """预热搜狗首页，获取 cookie，降低反爬概率"""
    try:
        _request_get('https://www.sogou.com/', timeout=8, referer='https://www.sogou.com/')
    except Exception:
        pass


_warmup_done = False


def fetch_sogou_web(query, max_results=8):
    """搜狗网页搜索（当前主源）：返回 [{title,url,source,summary,published}]"""
    global _warmup_done
    if not _warmup_done:
        _sogou_home_warmup()
        _warmup_done = True
        time.sleep(random.uniform(1.0, 2.0))

    url = 'https://www.sogou.com/web?query={}&page=1'.format(quote(query))
    items = []
    resp = _request_get(url, timeout=12, referer='https://www.sogou.com/')
    if not resp:
        return items
    text = resp.text
    if _is_antispider(text):
        database.log('collect', '搜狗网页触发验证: {}'.format(query), 'warn')
        return items

    # 搜狗结果一般在 h3 a
    results = re.findall(r'<h3[^>]*>\s*<a[^>]*href="([^"]+)"[^>]*>(.*?)</a>', text, re.S)
    skipped = 0
    for link, title in results[:max_results * 2]:
        title = _clean_text(title)
        if not title or len(title) < 10:
            skipped += 1
            continue
        if not link.startswith('http'):
            link = urljoin('https://www.sogou.com', link)
        # 解析真实 URL
        real_url = _resolve_sogou_link(link)
        # 过滤百科/知道/问答等低质量
        LOW_QUALITY = ['baike.baidu.com', 'zhidao.baidu.com', 'zhihu.com/question',
                       'wenku.baidu.com', 'tieba.baidu.com']
        if any(d in real_url for d in LOW_QUALITY):
            skipped += 1
            continue
        items.append({'title': title, 'url': real_url, 'source': '搜狗',
                      'published': '', 'summary': ''})
        if len(items) >= max_results:
            break
    database.log('collect', '搜狗[{}] 原始结果{}条 跳过{}条 有效{}条'.format(query, len(results), skipped, len(items)), 'ok')
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
    综合采集一个查询（当前策略）：
    1. 搜狗网页搜索（主源，国内机房访问稳定，真实URL可解析）
    2. 必应中国（辅源）
    3. 重点厂商官网（补充权威信息）
    4. 百度资讯（备用，常被验证拦截）
    返回去重后的结果列表
    """
    all_items = []
    counts = {'official': 0, 'baidu': 0, 'bing': 0, 'sogou': 0}

    # 1. 搜狗（主源）
    sogou = fetch_sogou_web(query, max_results=max_results)
    counts['sogou'] = len(sogou)
    all_items.extend(sogou)
    time.sleep(1.0)

    # 2. 必应（辅源）
    bing = fetch_bing(query, max_results=max_results)
    counts['bing'] = len(bing)
    all_items.extend(bing)
    time.sleep(0.8)

    # 3. 官网
    if vendor_name:
        official = fetch_vendor_official(vendor_name, max_results=max_results)
        counts['official'] = len(official)
        all_items.extend(official)
        time.sleep(0.5)

    # 4. 百度资讯（备用）
    baidu = fetch_baidu_news(query, max_results=max_results)
    counts['baidu'] = len(baidu)
    all_items.extend(baidu)

    # 去重：同 URL 只留一个；优先保留有来源的
    seen = {}
    for it in all_items:
        url = it.get('url', '')
        if not url:
            continue
        if url not in seen or (it.get('source') and not seen[url].get('source')):
            seen[url] = it

    results = list(seen.values())[:max_results]
    for r in results:
        if not r.get('source'):
            r['source'] = '网络'
    database.log('collect', '聚合[{}] 搜狗{} 必应{} 官网{} 百度{} 去重后{}'.format(
        query, counts['sogou'], counts['bing'], counts['official'], counts['baidu'], len(results)), 'ok')
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


# ==================== 诊断工具 ====================

def diagnose(query='海康威视 数字孪生'):
    """
    对单个查询做诊断，返回各数据源原始结果（不写入数据库）。
    用于后台排查云端反爬/解析问题。
    """
    out = {'query': query, 'sources': {}}
    # 百度
    try:
        baidu = fetch_baidu_news(query, max_results=5)
        out['sources']['baidu'] = {
            'count': len(baidu),
            'samples': [{'title': i['title'], 'source': i['source'], 'url': i['url']} for i in baidu[:3]]
        }
    except Exception as e:
        out['sources']['baidu'] = {'error': str(e)}
    time.sleep(0.5)
    # 必应
    try:
        bing = fetch_bing(query, max_results=5)
        out['sources']['bing'] = {
            'count': len(bing),
            'samples': [{'title': i['title'], 'source': i['source'], 'url': i['url']} for i in bing[:3]]
        }
    except Exception as e:
        out['sources']['bing'] = {'error': str(e)}
    # 搜狗
    try:
        sogou = fetch_sogou_web(query, max_results=5)
        out['sources']['sogou'] = {
            'count': len(sogou),
            'samples': [{'title': i['title'], 'source': i['source'], 'url': i['url']} for i in sogou[:3]]
        }
    except Exception as e:
        out['sources']['sogou'] = {'error': str(e)}
    return out


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
        # 夜间低频率：3~5 秒随机间隔，降低被封概率（腾讯云机房 IP 需要更慢）
        time.sleep(random.uniform(3.0, 5.0))

    _update_progress(running=False, done=len(queries), added=added,
                     errors=errors, current='', finished_at=database.now_str())
    database.log('collect', '新增 {} 条 / 查询 {} 组 / 错误 {}'.format(
        added, len(queries), errors), 'ok' if errors == 0 else 'warn')
    return added


if __name__ == '__main__':
    database.init_db()
    n = collect_once()
    print('本次采集新增 {} 条'.format(n))
