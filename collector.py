# -*- coding: utf-8 -*-
"""行业情报雷达 - 采集器
主数据源：搜狗微信搜索（免费、国内可用、覆盖厂商公众号文章）
备用数据源：搜狗网页搜索
"""
import re
import time
import html as html_mod
from urllib.parse import quote, urljoin

import requests

import config
import database

HEADERS = {
    'User-Agent': ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
                   '(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'),
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
    'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.5',
}


def _clean_text(s):
    """清洗HTML标签和实体"""
    if not s:
        return ''
    s = html_mod.unescape(s)
    s = re.sub(r'<[^>]+>', ' ', s)
    s = re.sub(r'\s+', ' ', s).strip()
    return s


def _is_antispider(html_text):
    """检测是否被反爬验证码拦截"""
    if 'antispider' in html_text or '请输入验证码' in html_text \
            or '安全验证' in html_text or 'seccodeInput' in html_text:
        return True
    return False


def fetch_sogou_weixin(query, max_results=8):
    """搜狗微信文章搜索：返回 [{title,url,source,summary,published}]"""
    url = 'https://weixin.sogou.com/weixin?type=2&query={}&ie=utf8'.format(quote(query))
    items = []
    try:
        resp = requests.get(url, headers=HEADERS, timeout=8,
                            cookies={'SUV': 'test'})
        resp.raise_for_status()
        text = resp.text
        if _is_antispider(text):
            database.log('collect', '搜狗微信触发验证码: {}'.format(query), 'warn')
            return items
        # 标题（含uigs标记）
        blocks = re.findall(
            r'<div class="txt-box">(.*?)(?=<div class="txt-box">|</div>\s*</div>|$)', text, re.S)
        if not blocks:
            # 回退：直接按 h3 切分
            blocks = re.findall(r'<div class="news-box">(.*?)(?=<div class="news-box">|$)', text, re.S)
        for block in blocks[:max_results]:
            m_title = re.search(r'<h3>\s*<a[^>]*href="([^"]+)"[^>]*>(.*?)</a>\s*</h3>', block, re.S)
            if not m_title:
                m_title = re.search(r'<a[^>]*uigs="article_title[^"]*"[^>]*href="([^"]+)"[^>]*>(.*?)</a>', block, re.S)
            if not m_title:
                continue
            link = m_title.group(1)
            title = _clean_text(m_title.group(2))
            if not link.startswith('http'):
                link = 'https://weixin.sogou.com' + link
            # 公众号名称（来源）
            m_acct = re.search(r'class="account"[^>]*>(.*?)</a>', block, re.S) \
                or re.search(r'uigs="article_account[^"]*"[^>]*>(.*?)</a>', block, re.S)
            source = _clean_text(m_acct.group(1)) if m_acct else ''
            # 摘要
            m_sum = re.search(r'class="txt-info"[^>]*>(.*?)</p>', block, re.S)
            summary = _clean_text(m_sum.group(1)) if m_sum else ''
            # 发布时间
            m_time = re.search(r'class="s2"[^>]*>(.*?)</', block, re.S)
            published = _clean_text(m_time.group(1)) if m_time else ''
            if title:
                items.append({
                    'title': title,
                    'url': link,
                    'source': source,
                    'published': published,
                    'summary': summary,
                })
    except Exception as e:
        database.log('collect', '搜狗微信查询失败: {} | {}'.format(query, e), 'error')
    return items


def fetch_sogou_web(query, max_results=6):
    """搜狗网页搜索（备用数据源）"""
    url = 'https://www.sogou.com/web?query={}'.format(quote(query))
    items = []
    try:
        resp = requests.get(url, headers=HEADERS, timeout=8)
        resp.raise_for_status()
        text = resp.text
        if _is_antispider(text):
            return items
        results = re.findall(
            r'<div class="vrwrap">(.*?)(?=<div class="vrwrap">|$)', text, re.S)
        if not results:
            results = re.findall(r'<h3[^>]*>\s*<a[^>]*href="([^"]+)"[^>]*>(.*?)</a>', text, re.S)
            for link, title in results[:max_results]:
                title = _clean_text(title)
                if not title:
                    continue
                if not link.startswith('http'):
                    link = urljoin('https://www.sogou.com', link)
                items.append({'title': title, 'url': link, 'source': '搜狗',
                              'published': '', 'summary': ''})
            return items
        for block in results[:max_results]:
            m = re.search(r'<h3[^>]*>\s*<a[^>]*href="([^"]+)"[^>]*>(.*?)</a>', block, re.S)
            if not m:
                continue
            link = m.group(1)
            title = _clean_text(m.group(2))
            if not link.startswith('http'):
                link = urljoin('https://www.sogou.com', link)
            m_sum = re.search(r'class="text-layout"[^>]*>(.*?)</div>', block, re.S) \
                or re.search(r'class="str_info"[^>]*>(.*?)</div>', block, re.S)
            summary = _clean_text(m_sum.group(1)) if m_sum else ''
            if title:
                items.append({'title': title, 'url': link, 'source': '搜狗',
                              'published': '', 'summary': summary})
    except Exception as e:
        database.log('collect', '搜狗网页查询失败: {} | {}'.format(query, e), 'error')
    return items


def fetch_all(query, max_results=8):
    """多数据源采集：优先搜狗微信，备用搜狗网页"""
    items = fetch_sogou_weixin(query, max_results)
    if not items:
        items = fetch_sogou_web(query, max_results)
    return items


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
    if any(k in text for k in ['展会', '大会', '论坛', '峰会', '博览会', 'EBC']):
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


# 异步采集进度（供后台轮询）
PROGRESS = {
    'running': False,
    'total': 0,
    'done': 0,
    'added': 0,
    'errors': 0,
    'current': '',
    'finished_at': '',
}


def collect_status():
    """返回当前采集进度（副本）"""
    return dict(PROGRESS)


def collect_once():
    """执行一次采集，返回新增条数（带实时进度更新）"""
    added = 0
    errors = 0
    queries = []

    # 厂商查询（搜狗微信搜索公众号文章）
    for vendor in config.VENDORS:
        for kw in vendor['keywords']:
            queries.append((vendor['name'], kw, vendor['name']))

    # 行业专项查询
    for q in config.INDUSTRY_QUERIES:
        queries.append(('', q, ''))

    PROGRESS['running'] = True
    PROGRESS['total'] = len(queries)
    PROGRESS['done'] = 0
    PROGRESS['added'] = 0
    PROGRESS['errors'] = 0
    PROGRESS['current'] = ''
    PROGRESS['finished_at'] = ''

    total_queries = len(queries)
    for i, (vendor, query, vendor_name) in enumerate(queries):
        PROGRESS['done'] = i
        PROGRESS['current'] = query
        try:
            items = fetch_all(query, config.MAX_PER_QUERY)
            for it in items:
                if not it['title']:
                    continue
                industry = detect_industry(it['title'], it['summary'])
                rel = score_relevance(it['title'], it['summary'])
                tags = detect_tags(it['title'], it['summary'])
                ok = database.add_intelligence({
                    'date': database.today_str(),
                    'vendor': vendor_name,
                    'industry': industry,
                    'title': it['title'],
                    'source': it['source'],
                    'url': it['url'],
                    'summary': (it['summary'] or '')[:200],
                    'description': it['summary'],
                    'relevance': rel,
                    'tags': tags,
                })
                if ok:
                    added += 1
        except Exception:
            errors += 1
        time.sleep(1.2)  # 搜狗限频

    PROGRESS['done'] = total_queries
    PROGRESS['added'] = added
    PROGRESS['errors'] = errors
    PROGRESS['running'] = False
    PROGRESS['current'] = ''
    PROGRESS['finished_at'] = database.now_str()
    database.log('collect', '新增 {} 条 / 查询 {} 组 / 错误 {}'.format(
        added, total_queries, errors), 'ok' if errors == 0 else 'warn')
    return added


if __name__ == '__main__':
    database.init_db()
    n = collect_once()
    print('本次采集新增 {} 条'.format(n))
