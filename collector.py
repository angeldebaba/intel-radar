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
from datetime import datetime, timedelta
from urllib.parse import quote, urljoin, urlparse

import requests
from bs4 import BeautifulSoup  # 使用标准库 html.parser，无需 lxml

import config
import database
import ai

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


def _extract_og_image(html_text, base_url=''):
    """从文章页提取 og:image 或正文首图，限定 jpg/png/webp。
    兜底首图优先取与页面同注册域的（防轮播广告图被当首图引入域名白名单）。"""
    if not html_text:
        return ''
    # meta 提取（og:image / twitter:image）：站点的默认占位图/logo 也算噪音，降级走兜底
    for meta_pat in (
            r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)["\']',
            r'<meta[^>]+name=["\']twitter:image["\'][^>]+content=["\']([^"\']+)["\']'):
        m = re.search(meta_pat, html_text, re.I)
        if not m:
            continue
        u = m.group(1).strip()
        low = u.lower()
        if any(n in low for n in _IMG_NOISE) or any(p in low for p in _IMG_PATH_NOISE):
            continue
        return u
    # 兜底：取正文首张干净图（跳过 logo/图标/皮肤目录），同注册域优先
    page_d = _reg_domain(urlparse(base_url).netloc) if base_url else ''
    same_d, first = '', ''
    for m in re.finditer(r'<img[^>]+src=["\'](https?://[^"\']+\.(?:jpg|jpeg|png|webp))["\']', html_text, re.I):
        u = m.group(1).strip()
        low = u.lower()
        if any(n in low for n in _IMG_NOISE) or any(p in low for p in _IMG_PATH_NOISE):
            continue
        if not first:
            first = u
        if page_d and _reg_domain(urlparse(u).netloc) == page_d:
            same_d = u
            break
    return same_d or first


def _enrich_article_image(item, timeout=6):
    """异步补充缩略图：抓文章页 → 解析 og:image"""
    if item.get('image'):
        return
    url = item.get('url', '')
    if not url.startswith('http'):
        return
    try:
        resp = _request_get(url, timeout=timeout, referer='https://www.sogou.com/')
        if not resp:
            return
        img = _extract_og_image(resp.text[:200000], resp.url)
        if img and img.startswith('http'):
            item['image'] = img
    except Exception:
        pass


# ==================== 原文媒体提取（图片/视频嵌入卡片） ====================

# 图片噪音特征：logo/图标/头像/表情/二维码/精灵图等
_IMG_NOISE = ('logo', 'icon', 'sprite', 'avatar', 'emoji', 'qrcode', 'wechat',
              'share_', 'button', 'banner_ad', 'ad_', 'pixel', 'spacer',
              'loading', 'placeholder', 'blank', 'default', '1x1', 'beacon',
              'symbol', 'cert', 'scan.', 'badge', 'medal')

# 图片噪音路径（目录级，比文件名更稳）：皮肤/样式/模板/控件目录全是非正文图
_IMG_PATH_NOISE = ('/skin/', '/css/', '/style/', '/templates/', '/widget/',
                   '/common/', '/images/logo', '/ads/', '/ad/', '/emoji/')

# 常见二级 TLD（注册域需取三段，如 xxx.com.cn）
_DOUBLE_TLDS = {'com.cn', 'net.cn', 'org.cn', 'gov.cn', 'edu.cn',
                'com.hk', 'com.tw', 'co.jp', 'com.au', 'co.uk', 'com.sg'}


def _reg_domain(host):
    """取注册域（主域）：news.ikanchai.com -> ikanchai.com；a.b.com.cn -> b.com.cn"""
    host = (host or '').lower().strip('.')
    if not host:
        return ''
    parts = host.split('.')
    if len(parts) >= 3 and '.'.join(parts[-2:]) in _DOUBLE_TLDS:
        return '.'.join(parts[-3:])
    return '.'.join(parts[-2:]) if len(parts) >= 2 else host


def _filter_foreign_imgs(imgs, base_url, og_url=''):
    """剔除与页面不同注册域的图床图（轮播广告/推荐位常用独立投放域）。
    全部跨域时原样返回——图片独立托管在图床的正规站（阿里云建站等）不误伤。"""
    if not imgs:
        return imgs
    page_d = _reg_domain(urlparse(base_url).netloc) if base_url else ''
    og_d = _reg_domain(urlparse(og_url).netloc) if og_url else ''
    if not page_d:
        return imgs
    allowed = {d for d in (page_d, og_d) if d}
    kept = [u for u in imgs if _reg_domain(urlparse(u).netloc) in allowed]
    return kept if kept else imgs

# 支持内嵌播放的视频平台（iframe src 域名特征）
_VIDEO_EMBED_HOSTS = ('player.bilibili.com', 'www.bilibili.com/video',
                      'v.qq.com/txp/iframe', 'v.qq.com/x/cover', 'v.qq.com/x/page',
                      'player.youku.com', 'youku.com/embed',
                      'www.youtube.com/embed', 'ixigua.com/embed')

_VIDEO_EXT = ('.mp4', '.webm', '.m3u8', '.mov')

# 正文外链追溯：跳过这些域名（搜索引擎/微信系/社交平台/CDN，追了也提不到视频）
_REF_SKIP_HOSTS = ('sogou.com', 'baidu.com', 'bing.com', 'google.', 'so.com', '360.cn',
                   'qq.com', 'qpic.cn', 'weibo.com', 'zhihu.com', 'csdn.net',
                   'toutiao.com', 'douyin.com', 'kuaishou.com', 'github.com',
                   'gitee.com', 'youtube.com', 'facebook.com', 'twitter.com', 'x.com',
                   'w3.org', 'gov.cn', 'edu.cn', 'mil.cn', 'cdn-static-pages',
                   'weixinbridge.com', 'unpkg.com', 'jsdelivr.net', 'cdnjs.cloudflare.com',
                   'bootcdn.net', 'staticfile.org')
# 静态资源后缀：这些"链接"是脚本/样式/字体，追了也没媒体
_REF_SKIP_EXT = ('.js', '.css', '.json', '.woff', '.woff2', '.ttf', '.eot', '.svg',
                 '.ico', '.xml', '.txt', '.rss')
# 视频页直判：正文提到这些平台的视频页链接时无需抓页面，直接转 embed 播放器
_VIDEO_PAGE_PATTERNS = (
    (r'bilibili\.com/video/(BV[\w]+)',
     'https://player.bilibili.com/player.html?bvid=%s&autoplay=0'),
    (r'v\.qq\.com/x/(?:page|cover)/(\w+)',
     'https://v.qq.com/txp/iframe/player.html?vid=%s'),
)


def _unescape_js(text):
    """解码 document.write JS 里的 \\uXXXX / \\xXX 转义，还原成 HTML 文本"""
    if not text:
        return ''

    def _rep(m):
        return chr(int(m.group(1), 16))
    text = re.sub(r'\\u([0-9a-fA-F]{4})', _rep, text)
    text = re.sub(r'\\x([0-9a-fA-F]{2})', _rep, text)
    return text.replace('\\"', '"').replace("\\'", "'")


def _expand_accel_shell(resp):
    """破解"网站加速"反爬壳（阿里云云速建站等）：
    真实页面只返回几百字节空壳 + <script src='.../xxx.Body.js'>（document.write 全量内容）。
    命中时抓 Body.js 并解码还原真实 HTML；否则原样返回 resp.text。"""
    text = resp.text or ''
    if len(text) > 4000:
        return text
    m = re.search(r'<script[^>]+src=[\'"]([^\'"]*cdn-static-pages[^\'"]*Body\.js[^\'"]*)[\'"]',
                  text, re.I)
    if not m:
        return text
    try:
        r2 = _request_get(m.group(1), timeout=8, referer=resp.url, retries=1)
        if not r2:
            return text
        expanded = _unescape_js(r2.text)
        return expanded if len(expanded) > len(text) else text
    except Exception:
        return text


def _norm_media_url(u, base_url):
    """规范化媒体链接：相对路径转绝对；无效返回 ''"""
    if not u:
        return ''
    u = u.strip().strip('\'"')
    if u.startswith('//'):
        u = 'https:' + u
    elif u.startswith('/'):
        u = urljoin(base_url, u)
    elif not u.startswith('http'):
        u = urljoin(base_url, u)
    u = u.replace('&amp;', '&')
    p = urlparse(u)
    if p.scheme not in ('http', 'https') or not p.netloc:
        return ''
    return u


def _extract_media(html_text, base_url):
    """从文章页 HTML 提取正文图片与视频链接（未校验有效性）
    返回 {'images': [...], 'videos': [{'url':..., 'type': 'video'|'embed'}]}"""
    out = {'images': [], 'videos': []}
    if not html_text:
        return out
    text = html_text[:400000]

    # ---- 视频：video/source 标签与 mp4 直链（最可靠，优先保留） ----
    seen_v = set()
    for m in re.finditer(r'<(?:video|source)[^>]+src=["\']([^"\']+)["\']', text, re.I):
        u = _norm_media_url(m.group(1), base_url)
        if u and u.lower().split('?')[0].endswith(_VIDEO_EXT) and u not in seen_v:
            seen_v.add(u)
            out['videos'].append({'url': u, 'type': 'video'})
    # 兜底：正文里裸露的 mp4 直链（JSON 数据里常见）
    if not out['videos']:
        for m in re.finditer(r'https?://[^\s"\'<>]+?\.mp4', text, re.I):
            u = _norm_media_url(m.group(0), base_url)
            if u and u not in seen_v and 'logo' not in u.lower():
                seen_v.add(u)
                out['videos'].append({'url': u, 'type': 'video'})
            if len(out['videos']) >= config.MEDIA_MAX_VIDEOS:
                break

    # ---- 视频：iframe 播放器（B站/腾讯/优酷/YouTube 等） ----
    for m in re.finditer(r'<iframe[^>]+src=["\']([^"\']+)["\']', text, re.I):
        u = _norm_media_url(m.group(1), base_url)
        if u and any(h in u for h in _VIDEO_EMBED_HOSTS) and u not in seen_v:
            # B站普通页转播放器页（可直接 iframe）
            m2 = re.search(r'bilibili\.com/video/(BV[\w]+)', u)
            if m2:
                u = 'https://player.bilibili.com/player.html?bvid=%s&autoplay=0' % m2.group(1)
            seen_v.add(u)
            out['videos'].append({'url': u, 'type': 'embed'})
    out['videos'] = out['videos'][:config.MEDIA_MAX_VIDEOS]

    # ---- 图片：og:image 优先 ----
    imgs = []
    og = _extract_og_image(text, base_url)
    if og:
        og = _norm_media_url(og, base_url)
        if og:
            imgs.append(og)
    # 正文 img 标签（含懒加载 data-src/data-original）
    for m in re.finditer(
            r'<img[^>]+src=["\']([^"\']+)["\'][^>]*>', text, re.I):
        u = m.group(0)
        src = _norm_media_url(m.group(1), base_url)
        # 懒加载：src 是占位图时取 data-src
        if not src or 'data:image' in m.group(1):
            m2 = re.search(r'data-(?:src|original|lazy-src)=["\']([^"\']+)["\']', u, re.I)
            if m2:
                src = _norm_media_url(m2.group(1), base_url)
        if not src:
            continue
        low = src.lower()
        # 过滤：动图表情/图标特征/皮肤目录/明显小图
        if low.endswith('.gif') or 'data:image' in low:
            continue
        if any(n in low for n in _IMG_NOISE):
            continue
        if any(p in low for p in _IMG_PATH_NOISE):
            continue
        # width/height 属性明显过小的跳过（图标/占位）
        mw = re.search(r'width=["\']?(\d+)', u, re.I)
        mh = re.search(r'height=["\']?(\d+)', u, re.I)
        if (mw and int(mw.group(1)) < 120) or (mh and int(mh.group(1)) < 80):
            continue
        imgs.append(src)
    # 去重保序
    seen = set()
    for u in imgs:
        if u and u not in seen:
            seen.add(u)
            out['images'].append(u)
    # 跨域过滤：剔除与页面/og 不同注册域的图（轮播广告/推荐位常用独立投放域）
    out['images'] = _filter_foreign_imgs(out['images'], base_url, og)
    return out


def _check_media_url(url, timeout=None):
    """校验媒体链接有效性：HEAD 请求 2xx/3xx 且非极小文件；
    HEAD 被拒(405)时降级 GET 流式探测。失败返回 False。"""
    timeout = timeout or config.MEDIA_CHECK_TIMEOUT
    try:
        r = _session.head(url, timeout=timeout, allow_redirects=True,
                          headers=_random_ua())
        code = r.status_code
        if code == 405 or code == 403:
            # 部分站点禁 HEAD，降级 GET 只看状态码
            rr = _session.get(url, timeout=timeout, allow_redirects=True,
                              stream=True, headers=_random_ua())
            code = rr.status_code
            clen = int(rr.headers.get('Content-Length') or 0)
            rr.close()
            return code < 400 and (clen == 0 or clen > 2048)
        if code >= 400:
            return False
        clen = int(r.headers.get('Content-Length') or 0)
        if 0 < clen <= 2048:  # 2KB 以下多为 1x1 像点/图标
            return False
        return True
    except Exception:
        return False


def _validate_media(media):
    """并发校验图片/视频链接，剔除失效链接（3 线程并发控制总耗时）"""
    if not media.get('images') and not media.get('videos'):
        return media
    from concurrent.futures import ThreadPoolExecutor
    urls = [v['url'] for v in media.get('videos', [])] + media.get('images', [])
    with ThreadPoolExecutor(max_workers=3) as ex:
        results = dict(zip(urls, ex.map(_check_media_url, urls)))
    media['images'] = [u for u in media['images'] if results.get(u)][:config.MEDIA_MAX_IMAGES]
    media['videos'] = [v for v in media['videos']
                       if results.get(v['url'])][:config.MEDIA_MAX_VIDEOS]
    return media


def _merge_media(dst, src):
    """把 src 的图片/视频合并进 dst（按 URL 去重，保序）"""
    if not src:
        return dst
    known = set(v['url'] for v in dst.get('videos', [])) | set(dst.get('images', []))
    for v in src.get('videos', []):
        if v.get('url') and v['url'] not in known:
            known.add(v['url'])
            dst.setdefault('videos', []).append(v)
    for img in src.get('images', []):
        if img and img not in known:
            known.add(img)
            dst.setdefault('images', []).append(img)
    return dst


def _follow_referenced_media(item, article_html, base_url, timeout=6):
    """方案A：追溯文章正文/摘要里提到的外部链接（公司官网/产品页），提取视频/图片。

    微信等 JS 渲染页静态 HTML 提不到媒体，但正文常提到官网，
    而官网产品页往往藏着宣传视频（含"网站加速"壳内 mp4）。"""
    out = {'images': [], 'videos': []}
    cand, seen_host = [], set()

    def _add(raw):
        u = (raw or '').strip()
        # 结尾省略号 = 搜索摘要截断的残缺 URL，跳过
        if u.endswith('..') or u.endswith('…'):
            return
        u = u.rstrip('.,;，。；)）】》>')
        if not u.startswith('http'):
            return
        # 静态资源（脚本/样式/字体）不是内容页，跳过
        path = u.split('?')[0].split('#')[0].lower()
        if path.endswith(_REF_SKIP_EXT):
            return
        try:
            host = urlparse(u).netloc.lower()
        except Exception:
            return
        # 需要像样的域名（带合法 TLD），排除搜索引擎/社交/微信系噪音站
        if not host or not re.search(r'\.[a-zA-Z]{2,}$', host):
            return
        if any(h in host for h in _REF_SKIP_HOSTS):
            return
        if host in seen_host:
            return
        seen_host.add(host)
        cand.append(u)

    # 候选按可靠性分级（先入队先追溯，名额有限）：
    # ① 摘要/描述里的裸 URL——搜索摘要明确提到的官网，最可靠
    # ② 正文 a 链接——作者主动放的引用
    # ③ 正文裸 URL——噪音最多（JS 里的统计/CDN 地址），垫底
    for src in (item.get('description') or '', item.get('summary') or ''):
        for m in re.finditer(r'https?://[\w.\-]+(?:/[\w.\-/%?=&#]*)?', src):
            _add(m.group(0))
    if article_html:
        for m in re.finditer(r'<a[^>]+href=["\']([^"\']+)["\']', article_html, re.I):
            _add(m.group(1))
    if article_html:
        for m in re.finditer(r'https?://[\w.\-]+(?:/[\w.\-/%?=&#]*)?', article_html):
            _add(m.group(0))

    if not cand:
        return out
    cand = cand[:config.MEDIA_FOLLOW_LINKS]

    for u in cand:
        # 视频平台视频页直判：链接本身就是视频页，转 embed 无需抓取
        hit_embed = False
        for pat, tpl in _VIDEO_PAGE_PATTERNS:
            m2 = re.search(pat, u)
            if m2:
                embed = tpl % m2.group(1)
                if embed not in [v['url'] for v in out['videos']]:
                    out['videos'].append({'url': embed, 'type': 'embed'})
                hit_embed = True
                break
        if hit_embed:
            continue
        # 抓引用页提取媒体（含"网站加速"壳自动展开）
        try:
            resp = _request_get(u, timeout=timeout, referer=base_url, retries=1)
            if not resp:
                continue
            page = _expand_accel_shell(resp)
            _merge_media(out, _extract_media(page, resp.url))
        except Exception:
            continue
        if len(out['videos']) >= config.MEDIA_MAX_VIDEOS:
            break
    out['videos'] = out['videos'][:config.MEDIA_MAX_VIDEOS]
    out['images'] = out['images'][:config.MEDIA_MAX_IMAGES]
    return out


def _enrich_article_media(item, timeout=6):
    """抓文章页提取缩略图 + 正文图片/视频（含有效性校验），写入 item['image']/item['media']。

    - 命中"网站加速"反爬壳时自动抓 Body.js 还原真实页面再提取
    - 正文提不到视频时，追溯正文/描述里提到的外部官网链接补媒体
    """
    url = item.get('url', '')
    if not url.startswith('http'):
        return
    try:
        resp = _request_get(url, timeout=timeout, referer='https://www.sogou.com/')
        page, base = ('', url)
        media = {'images': [], 'videos': []}
        if resp:
            page = _expand_accel_shell(resp)
            base = resp.url
            media = _extract_media(page, base)
            # og:image 已是精选缩略图候选，保底进 images 首位
            og = _norm_media_url(_extract_og_image(page[:200000], base), base)
            if og and og not in media['images']:
                media['images'].insert(0, og)
        # 正文没有视频时追溯外链（微信等 JS 渲染页、页面抓取失败均走这条路）
        if not media['videos']:
            _merge_media(media, _follow_referenced_media(item, page, base, timeout=timeout))
        if not media['images'] and not media['videos']:
            return
        media = _validate_media(media)
        if media['images']:
            item['image'] = media['images'][0]
        if media['images'] or media['videos']:
            item['media'] = {'images': media['images'], 'videos': media['videos']}
    except Exception:
        pass


def _is_antispider(text):
    """检测是否被反爬"""
    if not text:
        return True
    signs = ['请输入验证码', '安全验证', 'seccodeInput', 'antispider', '验证码', '访问过于频繁']
    return any(s in text for s in signs)


# ==================== 引擎熔断 ====================
# 某搜索引擎连续触发验证码时，本轮剩余查询自动跳过该引擎，
# 避免"越打越封"（被风控后继续请求只会延长封禁时间）。
_ENGINE_FAILS = {}
_ENGINE_DISABLED = set()
_ENGINE_FAIL_LIMIT = 2  # 连续 2 次验证码即熔断


def _report_engine_fail(name):
    """搜索引擎触发验证码时上报；达到阈值后本轮禁用"""
    _ENGINE_FAILS[name] = _ENGINE_FAILS.get(name, 0) + 1
    if _ENGINE_FAILS[name] >= _ENGINE_FAIL_LIMIT and name not in _ENGINE_DISABLED:
        _ENGINE_DISABLED.add(name)
        database.log('collect', '引擎[{}]连续触发验证码，本轮剩余查询自动跳过（避免延长封禁）'.format(name), 'warn')


def _engine_blocked(name):
    return name in _ENGINE_DISABLED


def reset_engines():
    """新一轮采集开始时重置熔断状态"""
    _ENGINE_FAILS.clear()
    _ENGINE_DISABLED.clear()


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


def _norm_date(s):
    """把 _extract_date 提取的原始字符串规范化为 YYYY-MM-DD；无效返回 ''"""
    if not s:
        return ''
    s = s.replace('年', '-').replace('月', '-').replace('日', '').replace('/', '-')
    parts = [p for p in s.split('-') if p]
    try:
        if len(parts) == 3:
            y, m, d = int(parts[0]), int(parts[1]), int(parts[2])
            if y < 100:  # 两位年份
                y += 2000
            return '%04d-%02d-%02d' % (y, m, d)
        if len(parts) == 2:  # MM-DD，默认当年
            m, d = int(parts[0]), int(parts[1])
            return '%04d-%02d-%02d' % (datetime.now().year, m, d)
    except (ValueError, IndexError):
        return ''
    return ''


def _is_stale(published, days=None):
    """发布时间超过 N 天视为过期旧文；无法解析发布时间的不拦截"""
    if not published:
        return False
    limit = days if days is not None else config.FRESH_DAYS
    try:
        pub = datetime.strptime(published, '%Y-%m-%d')
    except ValueError:
        return False
    return (datetime.now() - pub).days > limit


# ==================== 搜索引擎采集 ====================

def fetch_baidu_news(query, max_results=8):
    """百度资讯搜索：返回 [{title,url,source,summary,published}]"""
    items = []
    # 先预热百度首页，获取 cookie，降低被反爬概率
    _request_get('https://www.baidu.com/', timeout=8, referer='https://www.baidu.com/')
    time.sleep(random.uniform(0.3, 0.8))

    # 使用百度主站资讯搜索（rtt=4 按时间排序，优先返回最新新闻）
    url = 'https://www.baidu.com/s?rtt=4&bsst=1&cl=2&tn=news&word={}&ie=utf-8'.format(quote(query))
    resp = _request_get(url, timeout=12, referer='https://www.baidu.com/')
    if not resp:
        return items
    text = resp.text
    if _is_antispider(text):
        database.log('collect', '百度资讯触发验证: {}'.format(query), 'warn')
        _report_engine_fail('baidu')
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
    # ez5 时间过滤：限定最近 FRESH_DAYS 天（ez5 参数值为"epoch天数"区间，格式错误时必应自动忽略）
    _day = int(time.time() // 86400)
    _fresh = 'ex1%3a%22ez5_{}_{}%22'.format(max(0, _day - config.FRESH_DAYS), _day)
    url = ('https://cn.bing.com/search?q={}&setmkt=zh-CN&setlang=zh-CN'
           '&filters={}&FORM=BEHPTB').format(quote(query), _fresh)
    items = []
    resp = _request_get(url, timeout=12, referer='https://cn.bing.com/')
    if not resp:
        return items
    text = resp.text
    if _is_antispider(text):
        database.log('collect', '必应触发验证: {}'.format(query), 'warn')
        _report_engine_fail('bing')
        return items

    soup = BeautifulSoup(text, 'html.parser')
    NOISE_DOMAINS = ['baike.baidu.com', 'wikipedia.org', 'zhihu.com/question',
                     'www.zhihu.com', 'tieba.baidu.com', 'quote.eastmoney.com',
                     'download.', 'ws.com.cn']
    # 必应结果选择器：覆盖多版本 HTML 结构（b_algo / b_result / tilk / algo-sr）
    candidates = soup.select(
        'li.b_algo, div.b_algo, div.b_title, '
        'li.b_result, div.b_result, '
        'li.tilk, li[data-form], div.algo-sr')
    # 兜底：如果主选择器没命中，尝试 h2 a 祖先节点
    if not candidates:
        h2_links = soup.select('h2 a')
        candidates = []
        for a in h2_links:
            parent = a.find_parent(['li', 'div'])
            if parent:
                candidates.append(parent)
    skipped = 0
    for li in candidates[:max_results * 3]:
        a = li.select_one('h2 a') or li.select_one('a[href]')
        if not a:
            continue
        title = _clean_text(a.get_text())
        link = a.get('href', '')
        if not title or not link or len(title) < 8:
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
        # 放宽路径过滤：只过滤纯根路径（无子路径且标题无任何行业相关词）
        if path_lower.count('/') < 1 and not any(
                k in title for k in config.RELEVANCE_HIGH + config.RELEVANCE_MEDIUM):
            skipped += 1
            continue
        summary_el = li.select_one('p, div.b_caption, div.b_caption p')
        summary = _clean_text(summary_el.get_text() if summary_el else '')
        source_el = li.select_one('cite, div.b_attribution, span[dir="ltr"]')
        source = _clean_text(source_el.get_text() if source_el else '')
        if not source:
            source = '必应'
        published = _norm_date(_extract_date(_clean_text(li.get_text())))
        items.append({
            'title': title,
            'url': link,
            'source': source,
            'published': published,
            'summary': summary,
        })
        if len(items) >= max_results:
            break
    database.log('collect', '必应[{}] 原始结果{}条 跳过{}条 有效{}条'.format(
        query, len(candidates), skipped, len(items)), 'ok')
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

    # tsn=3 限定"一月内"结果，避免综合排序翻出多年前的旧文
    url = 'https://www.sogou.com/web?query={}&page=1&tsn=3'.format(quote(query))
    items = []
    resp = _request_get(url, timeout=12, referer='https://www.sogou.com/')
    if not resp:
        return items
    text = resp.text
    if _is_antispider(text):
        database.log('collect', '搜狗网页触发验证: {}'.format(query), 'warn')
        _report_engine_fail('sogou')
        return items

    # 搜狗结果：容器内 h3 a 为标题，摘要取常见摘要节点或容器文本
    soup = BeautifulSoup(text, 'html.parser')
    containers = soup.select('div.vrwrap, div.rb, div.results > div')
    skipped = 0
    for c in containers[:max_results * 3]:
        a = c.select_one('h3 a') or c.select_one('.vr-title a')
        if not a:
            continue
        title = _clean_text(a.get_text())
        if not title or len(title) < 10:
            skipped += 1
            continue
        link = a.get('href', '')
        if not link:
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
        # 提取摘要：优先摘要节点，其次容器文本去掉标题
        sum_el = c.select_one('.space-txt, .str-text-info, .str_info, .text-layout, .fz-mid')
        if sum_el:
            summary = _clean_text(sum_el.get_text())
        else:
            summary = _clean_text(c.get_text()).replace(title, '').strip()[:200]
        # 提取相对时间（如 "3小时前"、"2026-08-20"）
        published = ''
        time_el = c.select_one('.str_timeyin, .str-time, .vr-title ~ .citeurl, span[class*=time]')
        if time_el:
            published = _extract_date(_clean_text(time_el.get_text()))
        items.append({'title': title, 'url': real_url, 'source': '搜狗',
                      'published': published, 'summary': summary[:200]})
        if len(items) >= max_results:
            break
    database.log('collect', '搜狗[{}] 原始结果{}条 跳过{}条 有效{}条'.format(query, len(containers), skipped, len(items)), 'ok')
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
# 2026-08-23 更新失效 URL：biosphere3.com→smartyunzhou.com, 51world.com.cn 旧路径→新首页,
#   supermap.com.cn 旧路径→/cn, digihail.com 旧路径→首页, 移除 dahuatech/sensetime 的 404 方案页
OFFICIAL_CONFIG = [
    {'vendor': '海康威视', 'keywords': ['数字孪生', '视频融合', '三维', '智慧', '发布', '方案'],
     'pages': [
         'https://www.hikvision.com/cn/newsCenter/',
         'https://www.hikvision.com/cn/solutions/',
         'https://www.hikvision.com/cn/cases/',
     ]},
    {'vendor': '智汇云舟', 'keywords': ['数字孪生', '视频融合', '三维', '孪生', '视频', '智慧'],
     'pages': [
         'https://www.smartyunzhou.com/NewsInfoCategory?categoryId=583371',
     ]},
    {'vendor': '51WORLD', 'keywords': ['数字孪生', '三维', '发布', '方案', '智慧', '元宇宙'],
     'pages': [
         'https://www.51world.com.cn/',
     ]},
    {'vendor': '优锘科技', 'keywords': ['数字孪生', '三维', '可视化', '发布', '方案', '智慧'],
     'pages': [
         'https://www.uino.com/news',
         'https://www.uino.com/solution',
     ]},
    {'vendor': '大华股份', 'keywords': ['数字孪生', '视频融合', '三维', '智慧', '发布', '方案'],
     'pages': [
         'https://www.dahuatech.com/news/',
     ]},
    {'vendor': '华为', 'keywords': ['数字孪生', '三维', '智慧园区', '智慧建筑', '发布', '方案'],
     'pages': [
         'https://e.huawei.com/cn/news',
         'https://e.huawei.com/cn/solutions',
     ]},
    {'vendor': '超图软件', 'keywords': ['数字孪生', '三维GIS', 'GIS', '发布', '方案'],
     'pages': [
         'http://www.supermap.com.cn/cn',
         'https://www.supermap.com/zh-cn/a/news/',
     ]},
    {'vendor': '数字冰雹', 'keywords': ['数字孪生', '三维', '可视化', '发布', '方案'],
     'pages': [
         'http://www.digihail.com/',
     ]},
    {'vendor': '商汤科技', 'keywords': ['数字孪生', '三维', '重建', '发布', '方案', '智慧'],
     'pages': [
         'https://www.sensetime.com/cn/news',
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

    # 搜索引擎仅作为补充，降低风控概率（触发验证码达到阈值的引擎本轮自动跳过）
    if use_search:
        # 2. 搜狗（辅源）
        if _engine_blocked('sogou'):
            counts['sogou'] = -1  # -1 表示本轮已熔断跳过
        else:
            try:
                sogou = fetch_sogou_web(query, max_results=max_results)
                counts['sogou'] = len(sogou)
                all_items.extend(sogou)
            except Exception as e:
                database.log('collect', '搜狗异常: {} | {}'.format(query, e), 'warn')
            time.sleep(0.8)

        # 3. 必应（辅源）
        if _engine_blocked('bing'):
            counts['bing'] = -1
        else:
            try:
                bing = fetch_bing(query, max_results=max_results)
                counts['bing'] = len(bing)
                all_items.extend(bing)
            except Exception as e:
                database.log('collect', '必应异常: {} | {}'.format(query, e), 'warn')
            time.sleep(0.5)

        # 4. 百度（备用）
        if _engine_blocked('baidu'):
            counts['baidu'] = -1
        else:
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
    def _c(n):
        # -1 表示该引擎本轮已被熔断跳过
        return '熔断' if n < 0 else str(n)
    database.log('collect', '聚合[{}] 官网{} 搜狗{} 必应{} 百度{} 去重后{}'.format(
        query, counts['official'], _c(counts['sogou']), _c(counts['bing']),
        _c(counts['baidu']), len(results)), 'ok')
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
    reset_engines()  # 新一轮采集重置引擎熔断状态

    # 任务分组：
    # 阶段 A：先抓所有配置厂商的官网（主源）
    # 阶段 B：再用搜索引擎补充行业/厂商关键词
    vendor_queries = []
    for vendor in config.VENDORS:
        for kw in vendor['keywords']:
            vendor_queries.append((vendor['name'], kw))
    industry_queries = [(q, '') for q in config.INDUSTRY_QUERIES]
    total_steps = len(OFFICIAL_CONFIG) + len(vendor_queries) + len(industry_queries)

    _update_progress(running=True, total=total_steps, done=0, added=0,
                     errors=0, current='', finished_at='')

    # 全量去重
    url_seen = set()
    title_seen = set()
    step = 0
    media_enriched = 0  # 已抓媒体的文章页数（限额控制总耗时）

    def _add_items(items, vendor_name):
        nonlocal added, media_enriched
        # 第一步：基础清洗与去重，得到候选列表
        candidates = []
        for it in items:
            title = (it.get('title') or '').strip()
            url = (it.get('url') or '').strip()
            if not title or not url:
                continue
            if url in url_seen or title in title_seen:
                continue
            url_seen.add(url)
            title_seen.add(title)
            # 发布时间规范化（发布时间抓取失败时尝试从标题/正文提取）
            published = _norm_date(it.get('published') or '')
            if not published:
                published = _norm_date(_extract_date(title + ' ' + (it.get('summary') or '')))
            it['published'] = published
            # 时效过滤：能确定发布时间且超出 FRESH_DAYS 天的旧文直接丢弃
            if published and _is_stale(published):
                database.log('collect', '超龄旧文跳过({}发布): {}'.format(published, title[:40]), 'ok')
                continue
            # 正文预筛：导航页/聚合页/空壳页（正文<60字）没有可提炼内容，
            # AI 拿不到信息只会产出垃圾摘要，直接跳过省 API 调用
            body = (it.get('summary') or it.get('description') or '').strip()
            if len(body) < 60:
                database.log('collect', '预筛跳过无正文页: {}'.format(title[:40]), 'ok')
                continue
            candidates.append(it)
        if not candidates:
            return

        # 第二步：AI 批量提炼与过滤（无 Key / 调用失败返回 None，降级为关键词逻辑）
        ai_results = ai.analyze_batch(candidates)

        # 第三步：逐条入库
        for idx, it in enumerate(candidates):
            title = (it.get('title') or '').strip()
            url = (it.get('url') or '').strip()
            raw_summary = it.get('summary') or ''

            r = ai_results[idx] if ai_results else None
            if r is not None and r.get('score', -1) >= 0:
                # AI 判定：低于阈值丢弃（内容与主题对不上）
                if (not r.get('keep', True)) or r['score'] < config.AI_MIN_SCORE:
                    database.log('collect',
                                 'AI过滤丢弃[{}分]: {}'.format(r['score'], title[:45]), 'ok')
                    continue
                ai_summary = (r.get('summary') or '').strip()
                summary = ai_summary if ai_summary else raw_summary[:200]
                rel = max(1, r['score'])
                tags = r.get('tags') or detect_tags(title, raw_summary)
            else:
                # 降级：原关键词逻辑
                summary = raw_summary[:200]
                rel = score_relevance(title, raw_summary)
                tags = detect_tags(title, raw_summary)

            industry = detect_industry(title, raw_summary)

            # 媒体抓取：抓文章页提取图片/视频嵌入卡片（限额控制总耗时）
            if media_enriched < config.MEDIA_ENRICH_LIMIT:
                _enrich_article_media(it)
                media_enriched += 1

            ok = database.add_intelligence({
                'date': database.today_str(),
                'vendor': vendor_name,
                'industry': industry,
                'title': title,
                'source': it.get('source', ''),
                'url': url,
                'summary': summary,
                'description': raw_summary,
                'image': it.get('image', ''),
                'media': it.get('media') or {},
                'relevance': rel,
                'tags': tags,
                'published': it.get('published', ''),
            })
            if ok:
                added += 1

    # 阶段 A：逐个厂商抓官网
    official_counts = {}
    for cfg in OFFICIAL_CONFIG:
        vendor_name = cfg['vendor']
        _update_progress(done=step, current='官网:{}'.format(vendor_name))
        step += 1
        try:
            items = fetch_vendor_official(vendor_name, max_results=config.MAX_PER_QUERY)
            official_counts[vendor_name] = len(items)
            _add_items(items, vendor_name)
        except Exception as e:
            errors += 1
            database.log('collect', '官网采集失败: {} | {}'.format(vendor_name, e), 'warn')
        time.sleep(random.uniform(2.0, 4.0))

    # 阶段 B：搜索引擎补充（厂商关键词）
    # 效率优化：官网已采够 MAX_PER_QUERY 条的厂商跳过搜索引擎补充
    for vendor_name, query in vendor_queries:
        _update_progress(done=step, current=query)
        step += 1
        if official_counts.get(vendor_name, 0) >= config.MAX_PER_QUERY:
            continue
        try:
            items = fetch_query(query, vendor_name=vendor_name, max_results=config.MAX_PER_QUERY,
                                use_official=False, use_search=True)
            _add_items(items, vendor_name)
        except Exception as e:
            errors += 1
            database.log('collect', '查询失败: {} | {}'.format(query, e), 'warn')
        time.sleep(random.uniform(2.0, 3.5))

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
        time.sleep(random.uniform(2.0, 3.5))

    _update_progress(running=False, done=total_steps, added=added,
                     errors=errors, current='', finished_at=database.now_str())
    database.log('collect', '新增 {} 条 / 总步数 {} / 错误 {}'.format(
        added, total_steps, errors), 'ok' if errors == 0 else 'warn')
    return added


if __name__ == '__main__':
    database.init_db()
    n = collect_once()
    print('本次采集新增 {} 条'.format(n))
