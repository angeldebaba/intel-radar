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


def _request_get(url, timeout=10, headers=None, referer=None, retries=2):
    """通用 GET 请求，带随机 UA、session cookie、重试和异常处理"""
    last_err = None
    for attempt in range(retries + 1):
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
            last_err = e
            if attempt < retries:
                time.sleep(random.uniform(0.5, 1.5))
                continue
    database.log('collect', '请求失败(重试{}次): {} | {}'.format(retries, url[:80], last_err), 'warn')
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


# ==================== 厂商官网采集（主源）====================

# 常见无意义链接文本
_NOISE_WORDS = {'查看更多', '阅读更多', '更多', 'more', '详情', '点击了解',
                '了解详情', '立即咨询', '在线咨询', '免费试用', '首页', '返回',
                '上一页', '下一页', '联系我们', '关于我们', '新闻中心',
                '解决方案', '产品中心', '加入收藏', '设为首页'}

# 内容页路径特征
_CONTENT_PATHS = {'news', 'article', 'detail', 'solutions', 'solution',
                  'case', 'cases', 'product', 'products', 'about', 'intro',
                  'information', 'info', 'press', 'events', 'blog'}

# 厂商官网配置：主源，每天低频次抓取
OFFICIAL_CONFIG = [
    {'vendor': '海康威视', 'keywords': ['数字孪生', '视频融合', '三维', '智慧', '发布', '方案'],
     'pages': [
         'https://www.hikvision.com/cn/newsCenter/',
         'https://www.hikvision.com/cn/solutions/',
         'https://www.hikvision.com/cn/cases/',
     ]},
    {'vendor': '智汇云舟', 'keywords': ['数字孪生', '视频融合', '三维', '孪生', '视频', '智慧'],
     'pages': [
         'http://www.biosphere3.com/news/',
         'http://www.biosphere3.com/solution/',
         'http://www.biosphere3.com/case/',
     ]},
    {'vendor': '51WORLD', 'keywords': ['数字孪生', '三维', '发布', '方案', '智慧', '元宇宙'],
     'pages': [
         'http://www.51world.com.cn/news.html',
         'http://www.51world.com.cn/solution.html',
     ]},
    {'vendor': '优锘科技', 'keywords': ['数字孪生', '三维', '可视化', '发布', '方案', '智慧'],
     'pages': [
         'https://www.uino.com/news',
         'https://www.uino.com/solution',
     ]},
    {'vendor': '大华股份', 'keywords': ['数字孪生', '视频融合', '三维', '智慧', '发布', '方案'],
     'pages': [
         'https://www.dahuatech.com/news/',
         'https://www.dahuatech.com/solution/',
     ]},
    {'vendor': '华为', 'keywords': ['数字孪生', '三维', '智慧园区', '智慧建筑', '发布', '方案'],
     'pages': [
         'https://e.huawei.com/cn/news',
         'https://e.huawei.com/cn/solutions',
     ]},
    {'vendor': '超图软件', 'keywords': ['数字孪生', '三维GIS', 'GIS', '发布', '方案'],
     'pages': [
         'http://www.supermap.com.cn/news/',
         'http://www.supermap.com.cn/solution/',
     ]},
    {'vendor': '数字冰雹', 'keywords': ['数字孪生', '三维', '可视化', '发布', '方案'],
     'pages': [
         'http://www.digihail.com/news/',
         'http://www.digihail.com/solution/',
     ]},
    {'vendor': '商汤科技', 'keywords': ['数字孪生', '三维', '重建', '发布', '方案', '智慧'],
     'pages': [
         'https://www.sensetime.com/cn/news',
         'https://www.sensetime.com/cn/solution',
     ]},
]


def _in_block(el, names):
    """检查元素自身或祖先是否匹配指定 class/id 列表"""
    if not el:
        return False
    attr = ' '.join([el.get('class', ''), el.get('id', '')]).lower()
    return any(n.lower() in attr for n in names)


def _extract_item_date(el):
    """从元素本身或附近兄弟/父元素提取日期"""
    # 直接子元素常见日期标签
    for sel in ['.date', '.time', '.publish-time', '.post-date', '.news-date',
                'span.date', 'span.time', 'em', 'i']:
        node = el.select_one(sel)
        if node:
            d = _extract_date(_clean_text(node.get_text()))
            if d:
                return d
    # 父级附近
    parent = el.find_parent(['li', 'div', 'article'])
    if parent:
        d = _extract_date(_clean_text(parent.get_text()))
        if d:
            return d
    return ''


def _news_list_selectors():
    """常见新闻列表容器 CSS 选择器"""
    return [
        '.news-list', '.news-list-box', '.newslist', '.news_list',
        '.article-list', '.articlelist', '.list-news', '.list_news',
        '.items', '.item-list', '.item_list', '.list-item', '.list_item',
        '.content-list', '.content_list', '.media-list', '.news-box',
        '.newsbox', '.post-list', '.posts-list', '.blog-list',
        'ul.news', 'ul.articles', 'ul.items', 'ul.list',
        '.swiper-slide',  # 某些首页轮播
    ]


def _list_item_selectors():
    """常见单条新闻选择器"""
    return [
        'li', '.item', '.news-item', '.newsitem', '.news_li',
        '.article-item', '.list-item', '.media', '.post',
    ]


def _parse_structured_list(soup, base_url, vendor_name, keywords, max_results):
    """优先按常见新闻列表结构解析"""
    items = []
    seen = set()
    containers = soup.select(', '.join(_news_list_selectors()))
    if not containers:
        return items

    for container in containers:
        for li in container.select(', '.join(_list_item_selectors())):
            a = li.find('a', href=True)
            if not a:
                continue
            title = _clean_text(a.get_text()) or _clean_text(a.get('title', ''))
            if not title or len(title) < 8 or len(title) > 90:
                continue
            # 无意义文本过滤
            first = title[:8]
            if first in _NOISE_WORDS or title in _NOISE_WORDS:
                continue
            href = a['href'].strip()
            if href.startswith('#') or href.startswith('javascript:'):
                continue
            link = urljoin(base_url, href)
            if link in seen:
                continue

            # 关键词匹配：标题、a.title、附近文本
            context = (title + ' ' + _clean_text(a.get('title', '')) + ' ' +
                       _clean_text(li.get_text())).lower()
            if not any(kw.lower() in context for kw in keywords):
                # 若 URL 明显是内容页且路径含关键词，也保留
                path = urlparse(link).path.lower()
                if not any(cp in path for cp in _CONTENT_PATHS):
                    continue

            seen.add(link)
            summary = _clean_text(li.get_text()).replace(title, '').strip()[:200]
            published = _extract_item_date(li)
            items.append({
                'title': title,
                'url': link,
                'source': vendor_name,
                'published': published,
                'summary': summary,
            })
            if len(items) >= max_results:
                return items
    return items


def _parse_generic_links(soup, base_url, vendor_name, keywords, max_results):
    """兜底：遍历页面所有 a 标签，按关键词/路径筛选"""
    items = []
    seen = set()
    for a in soup.find_all('a', href=True):
        title = _clean_text(a.get_text()) or _clean_text(a.get('title', ''))
        if not title or len(title) < 10 or len(title) > 90:
            continue
        if title in _NOISE_WORDS or title[:8] in _NOISE_WORDS:
            continue
        href = a['href'].strip()
        if href.startswith('#') or href.startswith('javascript:'):
            continue
        link = urljoin(base_url, href)
        parsed = urlparse(link)
        path = parsed.path.lower()
        if parsed.path in ('', '/', '/index.html', '/index.htm', '/index.php'):
            continue

        # 命中关键词或 URL 是内容页
        ctx = (title + ' ' + _clean_text(a.get('title', ''))).lower()
        title_hit = any(kw.lower() in ctx for kw in keywords)
        path_hit = any('/' + cp in path or path.endswith('/' + cp) for cp in _CONTENT_PATHS)
        if not (title_hit or path_hit):
            continue
        if link in seen:
            continue
        seen.add(link)

        summary = ''
        parent = a.find_parent(['div', 'li', 'p', 'article'])
        if parent:
            summary = _clean_text(parent.get_text()).replace(title, '').strip()[:200]
        published = _extract_item_date(a)

        items.append({
            'title': title,
            'url': link,
            'source': vendor_name,
            'published': published,
            'summary': summary,
        })
        if len(items) >= max_results:
            break
    return items


def fetch_official_page(vendor_name, page_url, keywords, max_results=5):
    """解析单个厂商官网页面，优先结构化列表解析，失败再兜底"""
    items = []
    if not page_url:
        return items
    resp = _request_get(page_url, timeout=15, referer=page_url, retries=1)
    if not resp:
        return items
    text = resp.text
    if _is_antispider(text):
        database.log('collect', '官网触发验证: {} {}'.format(vendor_name, page_url), 'warn')
        return items

    soup = BeautifulSoup(text, 'html.parser')
    # 优先结构化列表解析
    items = _parse_structured_list(soup, resp.url, vendor_name, keywords, max_results)
    if not items:
        items = _parse_generic_links(soup, resp.url, vendor_name, keywords, max_results)
    return items


def fetch_vendor_official(vendor_name, max_results=8):
    """采集指定厂商的所有官网页面，合并去重"""
    items = []
    cfg = next((c for c in OFFICIAL_CONFIG if c['vendor'] == vendor_name), None)
    if not cfg:
        database.log('collect', '未配置官网: {}'.format(vendor_name), 'warn')
        return items

    for page_url in cfg['pages']:
        page_items = fetch_official_page(vendor_name, page_url, cfg['keywords'], max_results)
        items.extend(page_items)
        if len(items) >= max_results:
            break
        time.sleep(random.uniform(0.6, 1.2))

    # 按 URL 去重，保留来源
    seen = set()
    unique = []
    for it in items:
        url = it.get('url', '')
        if not url or url in seen:
            continue
        seen.add(url)
        unique.append(it)
    database.log('collect', '官网[{}] 有效{}条'.format(vendor_name, len(unique)), 'ok')
    return unique[:max_results]


def fetch_all_official(max_per_vendor=6):
    """采集所有配置厂商的官网，返回全部结果"""
    all_items = []
    for cfg in OFFICIAL_CONFIG:
        items = fetch_vendor_official(cfg['vendor'], max_results=max_per_vendor)
        all_items.extend(items)
        time.sleep(random.uniform(1.0, 2.0))
    return _dedup_by_url(all_items)


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


def fetch_query(query, vendor_name='', max_results=6, use_official=True, use_search=True):
    """
    综合采集一个查询（新策略）：
    1. 重点厂商官网（主源，权威、稳定、不被搜索引擎风控）
    2. 搜狗网页搜索（辅源，国内访问较好）
    3. 必应中国（辅源）
    4. 百度资讯（备用）
    返回去重后的结果列表
    """
    all_items = []
    counts = {'official': 0, 'baidu': 0, 'bing': 0, 'sogou': 0}

    # 1. 官网（主源）
    if use_official and vendor_name:
        official = fetch_vendor_official(vendor_name, max_results=max_results)
        counts['official'] = len(official)
        all_items.extend(official)
        time.sleep(0.5)

    # 搜索引擎仅作为补充，降低风控概率
    if use_search:
        # 2. 搜狗（辅源）
        try:
            sogou = fetch_sogou_web(query, max_results=max_results)
            counts['sogou'] = len(sogou)
            all_items.extend(sogou)
        except Exception as e:
            database.log('collect', '搜狗异常: {} | {}'.format(query, e), 'warn')
        time.sleep(0.8)

        # 3. 必应（辅源）
        try:
            bing = fetch_bing(query, max_results=max_results)
            counts['bing'] = len(bing)
            all_items.extend(bing)
        except Exception as e:
            database.log('collect', '必应异常: {} | {}'.format(query, e), 'warn')
        time.sleep(0.5)

        # 4. 百度（备用）
        try:
            baidu = fetch_baidu_news(query, max_results=max_results)
            counts['baidu'] = len(baidu)
            all_items.extend(baidu)
        except Exception as e:
            database.log('collect', '百度异常: {} | {}'.format(query, e), 'warn')

    # 去重：同 URL 只留一个；优先保留官网来源
    seen = {}
    for it in all_items:
        url = it.get('url', '')
        if not url:
            continue
        if url not in seen or (it.get('source') and it.get('source') != '网络'):
            seen[url] = it

    results = list(seen.values())[:max_results]
    for r in results:
        if not r.get('source'):
            r['source'] = '网络'
    database.log('collect', '聚合[{}] 官网{} 搜狗{} 必应{} 百度{} 去重后{}'.format(
        query, counts['official'], counts['sogou'], counts['bing'], counts['baidu'], len(results)), 'ok')
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
    vendor_name = query.split()[0] if query else ''

    # 官网
    try:
        official = fetch_vendor_official(vendor_name, max_results=5) if vendor_name else []
        out['sources']['official'] = {
            'count': len(official),
            'samples': [{'title': i['title'], 'source': i['source'], 'url': i['url']} for i in official[:3]]
        }
    except Exception as e:
        out['sources']['official'] = {'error': str(e)}
    time.sleep(0.5)

    # 搜狗
    try:
        sogou = fetch_sogou_web(query, max_results=5)
        out['sources']['sogou'] = {
            'count': len(sogou),
            'samples': [{'title': i['title'], 'source': i['source'], 'url': i['url']} for i in sogou[:3]]
        }
    except Exception as e:
        out['sources']['sogou'] = {'error': str(e)}
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
    time.sleep(0.5)

    # 百度
    try:
        baidu = fetch_baidu_news(query, max_results=5)
        out['sources']['baidu'] = {
            'count': len(baidu),
            'samples': [{'title': i['title'], 'source': i['source'], 'url': i['url']} for i in baidu[:3]]
        }
    except Exception as e:
        out['sources']['baidu'] = {'error': str(e)}
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
    """执行一次完整采集，返回新增条数。每天晚上低频率运行。"""
    added = 0
    errors = 0

    # 任务分组：
    # 阶段 A：先抓所有配置厂商的官网（主源）
    # 阶段 B：再用搜索引擎补充行业/厂商关键词
    vendor_queries = []
    for vendor in config.VENDORS:
        for kw in vendor['keywords']:
            vendor_queries.append((vendor['name'], kw))
    industry_queries = [(q, '') for q in config.INDUSTRY_QUERIES]
    total_steps = len(config.OFFICIAL_CONFIG) + len(vendor_queries) + len(industry_queries)

    _update_progress(running=True, total=total_steps, done=0, added=0,
                     errors=0, current='', finished_at='')

    # 全量去重
    url_seen = set()
    title_seen = set()
    step = 0

    def _add_items(items, vendor_name):
        nonlocal added
        for it in items:
            title = it.get('title', '').strip()
            url = it.get('url', '').strip()
            if not title or not url:
                continue
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

    # 阶段 A：逐个厂商抓官网
    for cfg in OFFICIAL_CONFIG:
        vendor_name = cfg['vendor']
        _update_progress(done=step, current='官网:{}'.format(vendor_name))
        step += 1
        try:
            items = fetch_vendor_official(vendor_name, max_results=config.MAX_PER_QUERY)
            _add_items(items, vendor_name)
        except Exception as e:
            errors += 1
            database.log('collect', '官网采集失败: {} | {}'.format(vendor_name, e), 'warn')
        time.sleep(random.uniform(2.0, 4.0))

    # 阶段 B：搜索引擎补充（厂商关键词）
    for vendor_name, query in vendor_queries:
        _update_progress(done=step, current=query)
        step += 1
        try:
            items = fetch_query(query, vendor_name=vendor_name, max_results=config.MAX_PER_QUERY,
                                use_official=False, use_search=True)
            _add_items(items, vendor_name)
        except Exception as e:
            errors += 1
            database.log('collect', '查询失败: {} | {}'.format(query, e), 'warn')
        time.sleep(random.uniform(3.0, 5.0))

    # 阶段 C：搜索引擎补充（行业关键词，不带厂商）
    for query, _ in industry_queries:
        _update_progress(done=step, current=query)
        step += 1
        try:
            items = fetch_query(query, vendor_name='', max_results=config.MAX_PER_QUERY,
                                use_official=False, use_search=True)
            _add_items(items, '')
        except Exception as e:
            errors += 1
            database.log('collect', '查询失败: {} | {}'.format(query, e), 'warn')
        time.sleep(random.uniform(3.0, 5.0))

    _update_progress(running=False, done=total_steps, added=added,
                     errors=errors, current='', finished_at=database.now_str())
    database.log('collect', '新增 {} 条 / 总步数 {} / 错误 {}'.format(
        added, total_steps, errors), 'ok' if errors == 0 else 'warn')
    return added


if __name__ == '__main__':
    database.init_db()
    n = collect_once()
    print('本次采集新增 {} 条'.format(n))
