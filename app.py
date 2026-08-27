# -*- coding: utf-8 -*-
"""行业情报雷达 - Flask 主应用
包含：公开页API / 后台管理API / 竞品分析引擎 / 每日调度
部署：gunicorn app:app 或 python app.py
"""
import json
import os
import re
import threading
import time
import uuid
from datetime import datetime, date, timedelta
from functools import wraps
from urllib.parse import urlparse

from flask import (Flask, render_template, request, jsonify, session,
                   send_from_directory, redirect)
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

import config
import database
import collector
import pusher
import ai
import daily_focus
import hot_stats

app = Flask(__name__)
app.secret_key = config.SECRET_KEY
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024  # 16MB上传限制

# ==================== 初始化 ====================
database.init_db()

# 首次启动把默认配置写入数据库
# 当代码升级（config_version 变化）时，重新同步时间/条数默认值
CONFIG_VERSION = '3'
if database.get_config('config_version') != CONFIG_VERSION:
    database.set_config('config_version', CONFIG_VERSION)
    database.set_config('vendors_configured', '1')
    database.set_config('coll_time', config.COLLECT_TIME)
    database.set_config('push_time', config.PUSH_TIME)
    database.set_config('push_top_n', str(config.PUSH_TOP_N))
    database.set_config('intel_retention_days', str(config.INTEL_RETENTION_DAYS))


# ==================== 鉴权 ====================
def require_admin(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not session.get('admin'):
            return jsonify({'ok': False, 'error': '未登录'}), 401
        return f(*args, **kwargs)
    return wrapper


# ==================== 访问统计埋点 ====================
# 高并发策略：请求线程只做入队（内存 append，微秒级），后台线程每 5 秒批量落库
_VISIT_QUEUE = []
_VISIT_LOCK = threading.Lock()
_VISIT_QUEUE_MAX = 5000          # 队列上限（防御性，超出丢弃最旧的）
_SEARCH_ENGINES = ('baidu.', 'google.', 'bing.', 'sogou.', 'so.com', 'sm.cn',
                   'quark', 'duckduckgo', 'yandex.', 'so.360.cn', 'qihoo')


def _classify_referrer(ref, host=''):
    """来源分类：direct 直接访问 / search 搜索引擎 / external 外部链接 / internal 站内跳转"""
    if not ref:
        return 'direct'
    try:
        rhost = urlparse(ref).netloc.lower()
    except Exception:
        return 'direct'
    if not rhost:
        return 'direct'
    if host and rhost == host.lower():
        return 'internal'
    if any(e in rhost for e in _SEARCH_ENGINES):
        return 'search'
    return 'external'


def _get_client_ip():
    """从请求头提取客户端真实 IP（CloudBase / CDN 场景 XFF 多跳取首个公网 IP）"""
    xff = request.headers.get('X-Forwarded-For', '')
    if xff:
        for ip in xff.split(','):
            ip = ip.strip()
            if ip and not ip.startswith(('10.', '172.16.', '172.17.', '172.18.',
                                         '172.19.', '172.20.', '172.21.', '172.22.',
                                         '172.23.', '172.24.', '172.25.', '172.26.',
                                         '172.27.', '172.28.', '172.29.', '172.30.',
                                         '172.31.', '192.168.', '127.')):
                return ip[:45]
        return xff.split(',')[0].strip()[:45]
    rip = (request.remote_addr or '')[:45]
    return rip


_IP_REGION_CACHE = {}          # ip -> '中国 广东 深圳'  （内存缓存，进程级）
_IP_CACHE_LOCK = threading.Lock()


def _is_private_ip(ip):
    return (not ip or ip.startswith(('10.', '172.16.', '172.17.', '172.18.', '172.19.',
        '172.20.', '172.21.', '172.22.', '172.23.', '172.24.', '172.25.', '172.26.',
        '172.27.', '172.28.', '172.29.', '172.30.', '172.31.', '192.168.', '127.')))


def _ip_region_batch(ips):
    """批量查询 IP 归属地（ip-api.com batch API，每批最多 100 条，带内存缓存）
    返回 dict: {ip: '中国 广东 深圳'  或 '' 查询失败}
    """
    result = {}
    pending = []
    with _IP_CACHE_LOCK:
        for ip in ips:
            if not ip or _is_private_ip(ip):
                result[ip] = '本地内网'
                continue
            if ip in _IP_REGION_CACHE:
                result[ip] = _IP_REGION_CACHE[ip]
            elif ip not in pending:
                pending.append(ip)
    # 批量查询 pending 中未缓存的 IP
    while pending:
        batch = pending[:100]
        pending = pending[100:]
        try:
            import urllib.request
            body = json.dumps(batch).encode()
            req = urllib.request.Request(
                'http://ip-api.com/batch?fields=status,country,regionName,city,query&lang=zh-CN',
                data=body, headers={'Content-Type': 'application/json'}, method='POST')
            with urllib.request.urlopen(req, timeout=5) as r:
                arr = json.loads(r.read().decode())
            for item in arr:
                ip = (item.get('query') or '').strip()
                if item.get('status') == 'success':
                    parts = [item.get('country', ''), item.get('regionName', ''),
                             item.get('city', '')]
                    region = ' '.join(p for p in parts if p)
                else:
                    region = ''
                result[ip] = region
                with _IP_CACHE_LOCK:
                    _IP_REGION_CACHE[ip] = region
        except Exception:
            for ip in batch:
                result[ip] = result.get(ip, '')
                with _IP_CACHE_LOCK:
                    if ip not in _IP_REGION_CACHE:
                        _IP_REGION_CACHE[ip] = ''
    return result


def _track_enqueue(rec):
    """入队（请求线程调用，必须无阻塞）"""
    with _VISIT_LOCK:
        if len(_VISIT_QUEUE) >= _VISIT_QUEUE_MAX:
            del _VISIT_QUEUE[:len(_VISIT_QUEUE) - _VISIT_QUEUE_MAX // 2]
        _VISIT_QUEUE.append(rec)


def _visit_flush_loop():
    """后台线程：批量落库 + 每日清理超龄访问数据"""
    last_purge = None
    while True:
        time.sleep(5)
        global _VISIT_QUEUE
        with _VISIT_LOCK:
            batch, _VISIT_QUEUE = _VISIT_QUEUE, []
        try:
            if batch:
                # 落库前批量富化 IP 归属地（仅在 hit 记录有 IP 且缺 region 时）
                hit_ips = {r.get('ip', '') for r in batch
                           if r.get('kind') == 'hit' and r.get('ip') and not r.get('region')}
                if hit_ips:
                    regions = _ip_region_batch(list(hit_ips))
                    for r in batch:
                        if r.get('kind') == 'hit' and r.get('ip') and not r.get('region'):
                            r['region'] = regions.get(r['ip'], '')
                database.record_visit_batch(batch)
        except Exception as e:
            # 落库失败不影响业务；丢本批避免阻塞（原始数据本就是统计用途）
            try:
                database.log('visit_flush_error', str(e)[:200], status='warn')
            except Exception:
                pass
        try:
            today = date.today().isoformat()
            if last_purge != today:
                last_purge = today
                database.purge_visit_log(config.VISIT_RETENTION_DAYS)
        except Exception:
            pass


threading.Thread(target=_visit_flush_loop, daemon=True, name='visit-flush').start()


@app.before_request
def _track_api_hits():
    """统计公开 API 接口访问（后台管理轮询接口与埋点接口本身不计）"""
    try:
        p = request.path
        if (request.method == 'GET' and p.startswith('/api/')
                and not p.startswith('/api/admin/') and p != '/api/track'):
            ref = request.referrer or ''
            ua = (request.headers.get('User-Agent') or '')
            _track_enqueue({
                'kind': 'hit', 'ts': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                'path': p, 'sid': (request.cookies.get('ir_sid') or '')[:64],
                'referrer': ref[:380],
                'rtype': _classify_referrer(ref, urlparse(request.host_url).netloc),
                'device': 'mobile' if re.search(r'Mobi|Android|iPhone', ua, re.I) else 'pc',
                'ip': _get_client_ip(),
            })
    except Exception:
        pass
    return None


@app.route('/api/track', methods=['POST'])
def api_track():
    """前端埋点上报：pageview（含来源/设备/会话）与 duration（停留时长）"""
    try:
        data = request.get_json(force=True, silent=True) or {}
    except Exception:
        data = {}
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    sid = str(data.get('sid', ''))[:64]
    if data.get('type') == 'duration':
        try:
            dur = int(float(data.get('duration', 0)))
        except (TypeError, ValueError):
            dur = 0
        if sid and 0 < dur <= 86400:
            _track_enqueue({'kind': 'duration', 'ts': now, 'sid': sid, 'duration': dur})
        return jsonify({'ok': True})
    # 默认 pageview
    ref = str(data.get('referrer', ''))[:380]
    _track_enqueue({
        'kind': 'hit', 'ts': now,
        'path': str(data.get('path', '/') or '/')[:190],
        'sid': sid,
        'referrer': ref,
        'rtype': _classify_referrer(ref, urlparse(request.host_url).netloc),
        'device': 'mobile' if data.get('device') == 'mobile' else 'pc',
        'ip': _get_client_ip(),
    })
    return jsonify({'ok': True})


@app.route('/api/admin/analytics')
@require_admin
def api_admin_analytics():
    """访问统计报表：period=day(近30日)/week(近12周)/month(近12月)"""
    period = request.args.get('period', 'day')
    if period == 'week':
        days = 84
    elif period == 'month':
        days = 365
    else:
        period, days = 'day', 30
    d = database.analytics_query(days)

    # 按周期分桶（visit_daily 已按日聚合，Python 侧分桶避免 SQL 方言差异）
    def bucket(dt_str):
        if period == 'day':
            return dt_str[5:]
        if period == 'month':
            return dt_str[:7]
        dt = datetime.strptime(dt_str, '%Y-%m-%d')
        y, w, _ = dt.isocalendar()
        return '%d-W%02d' % (y, w)

    buckets = {}
    for row in d['daily']:          # daily 已按 date 升序，桶保持时间顺序
        k = bucket(row['date'])
        b = buckets.setdefault(k, {'hits': 0, 'sessions': 0})
        b['hits'] += row['hits'] or 0
        b['sessions'] += row['sessions'] or 0
    trend = [{'label': k, 'hits': v['hits'], 'sessions': v['sessions']}
             for k, v in buckets.items()]

    src_map = {r['t']: r['c'] for r in d['sources']}
    total_hits = sum(src_map.values())
    summary = {
        'hits': total_hits,
        'sessions': sum((r['sessions'] or 0) for r in d['daily']),
        'avg_duration': round(float(d['sess'].get('avg_d') or 0)),
        'max_duration': int(d['sess'].get('max_d') or 0),
        'direct': src_map.get('direct', 0),
        'search': src_map.get('search', 0),
        'external': src_map.get('external', 0),
        'internal': src_map.get('internal', 0),
    }
    return jsonify({'ok': True, 'data': {
        'period': period, 'range_days': days,
        'summary': summary, 'trend': trend,
        'pages': d['pages'], 'sources': d['sources'],
        'referrers': d['referrers'], 'recent': d['recent'],
    }})


@app.route('/api/admin/visit-detail')
@require_admin
def api_admin_visit_detail():
    """访问明细分页列表：page / page_size / days 参数"""
    try:
        page = max(1, int(request.args.get('page', 1)))
    except (TypeError, ValueError):
        page = 1
    try:
        page_size = min(200, max(10, int(request.args.get('page_size', 50))))
    except (TypeError, ValueError):
        page_size = 50
    try:
        days = min(365, max(1, int(request.args.get('days', 30))))
    except (TypeError, ValueError):
        days = 30
    d = database.visit_detail_query(page=page, page_size=page_size, days=days)
    return jsonify({'ok': True, 'data': d})


# ==================== 页面 ====================
@app.route('/')
def index():
    return render_template('index.html')


@app.route('/favicon.ico')
def favicon():
    return '', 204


@app.route('/article/<int:iid>')
def article_view(iid):
    """原文存档查看页：从本地库渲染存档原文，防链接过期/反爬导致不可回看。
    无存档时回退跳转原站链接。"""
    row = database.query_one(
        'SELECT id, title, source, url, date, published, vendor FROM intelligence WHERE id='
        + database.PH, (iid,))
    if not row:
        return '记录不存在或已过保留期清理', 404
    arch = database.get_archive(iid)
    if not arch or not (arch.get('html') or arch.get('plain_text')):
        if row['url']:
            return redirect(row['url'])
        return '暂无原文存档', 404

    base_tag = ''
    if (arch.get('base_url') or '').startswith('http'):
        # base 让存档里的相对路径图片/链接按原站解析
        base_tag = '<base href="{}" target="_blank">'.format(esc(arch['base_url']))
    body_html = arch.get('html') or ''
    if not body_html:
        body_html = '<pre class="plain">{}</pre>'.format(esc(arch.get('plain_text') or ''))

    meta_bits = [esc(row['source'] or '网络'), esc(row['date'])]
    if row.get('published'):
        meta_bits.append('发布 ' + esc(row['published']))
    if row.get('vendor'):
        meta_bits.insert(0, esc(row['vendor']))
    orig_link = ('<a class="o" href="{}" target="_blank" rel="noopener">原站链接 ↗</a>'
                 .format(esc(row['url']))) if row['url'] else ''
    page = '''<!DOCTYPE html><html lang="zh-CN"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
{base}<title>{title} · 原文存档</title>
<style>
body{{margin:0;background:#0d1117;color:#d6dee6;font-family:"PingFang SC","Microsoft YaHei",sans-serif;line-height:1.8;}}
.wrap{{max-width:800px;margin:0 auto;padding:24px 16px 64px;}}
.bar{{display:flex;align-items:center;gap:12px;flex-wrap:wrap;padding:12px 16px;background:#161b22;border:1px solid #30363d;border-radius:10px;margin-bottom:16px;}}
.bar h1{{font-size:17px;font-weight:600;margin:0;flex:1;min-width:200px;color:#f0f6fc;}}
.bar .m{{font-size:12px;color:#8b949e;}}
.bar a{{color:#58a6ff;text-decoration:none;font-size:13px;}}
.bar a.b{{color:#8b949e;}}
.arc{{background:#161b22;border:1px solid #30363d;border-radius:10px;padding:20px 24px;overflow-wrap:break-word;}}
.arc img,.arc video{{max-width:100%;height:auto;border-radius:6px;}}
.arc iframe{{max-width:100%;aspect-ratio:16/9;border:0;border-radius:6px;}}
.arc a{{color:#58a6ff;}}
.arc pre.plain{{white-space:pre-wrap;font-family:inherit;}}
.ft{{text-align:center;color:#8b949e;font-size:12px;margin-top:14px;}}
</style></head><body><div class="wrap">
<div class="bar"><a class="b" href="/">← 返回雷达</a><h1>{title}</h1>{orig}</div>
<div class="arc">{body}</div>
<div class="ft">本地存档 · 采集于 {fetched} · intel-radar</div>
</div></body></html>'''.format(
        base=base_tag, title=esc(row['title']), orig=orig_link,
        body=body_html, fetched=esc((arch.get('fetched_at') or '')[:19]))
    return page


# ==================== 公开 API ====================
@app.route('/api/stats')
def api_stats():
    s = database.stats()
    s['collected_at'] = database.query_one(
        "SELECT MAX(collected_at) m FROM intelligence")['m'] or ''
    return jsonify({'ok': True, 'data': s})


@app.route('/api/stats/trend')
def api_stats_trend():
    """趋势仪表盘数据：最近7日每日新增 + 厂商分布Top12 + 标签分布"""
    from datetime import timedelta
    days = []
    base = datetime.now()
    for i in range(6, -1, -1):
        days.append((base - timedelta(days=i)).strftime('%Y-%m-%d'))
    ph = database.PH
    daily = {r['date']: r['c'] for r in database.query(
        'SELECT date, COUNT(*) AS c FROM intelligence WHERE date >= ' + ph + ' GROUP BY date',
        (days[0],))}
    vendors = database.query(
        "SELECT vendor, COUNT(*) AS c FROM intelligence WHERE vendor!='' "
        'GROUP BY vendor ORDER BY c DESC LIMIT 12')
    tags = {}
    for r in database.query('SELECT tags FROM intelligence'):
        for t in json.loads(r['tags'] or '[]'):
            tags[t] = tags.get(t, 0) + 1
    return jsonify({'ok': True, 'data': {
        'daily': [{'date': d, 'count': daily.get(d, 0)} for d in days],
        'vendors': vendors,
        'tags': [{'tag': k, 'count': v} for k, v in
                 sorted(tags.items(), key=lambda x: -x[1])],
    }})


@app.route('/api/intelligence')
def api_intelligence():
    """情报列表：支持 vendor/industry/tag/relevance/fav/q 过滤 + 滚动游标分页。

    - 默认按全部日期查询，采集时间由新到旧排序
    - 首次请求（无 cursor）：返回第一页（page_size 条）+ next_cursor
    - 带 cursor 请求：按游标继续加载更早记录（滚动增量加载）：
        sort=new → 按 (date, id) 游标，新→旧
        sort=rel → 按 (relevance, id) 游标，相关度高→低
    """
    where, args = [], []
    vendor = request.args.get('vendor', '')
    industry = request.args.get('industry', '')
    tag = request.args.get('tag', '')
    relevance = request.args.get('relevance', '')
    fav = request.args.get('fav', '')
    keyword = request.args.get('q', '')
    cursor = request.args.get('cursor', '')
    sort = request.args.get('sort', 'new')

    if vendor:
        where.append('vendor=?')
        args.append(vendor)
    if industry:
        where.append('industry=?')
        args.append(industry)
    if tag:
        where.append('tags LIKE ?')
        args.append('%{}%'.format(tag))
    if relevance:
        where.append('relevance>=?')
        args.append(int(relevance))
    if fav == '1':
        where.append('is_favorite=1')
    if keyword:
        where.append('(title LIKE ? OR summary LIKE ?)')
        args.extend(['%{}%'.format(keyword)] * 2)

    if sort == 'rel':
        order_sql = 'relevance DESC, id DESC'
    else:
        sort = 'new'
        order_sql = 'date DESC, id DESC'

    if cursor:
        # 滚动加载：按游标取更早/更低优先级的记录
        try:
            key, last_id = cursor.rsplit('_', 1)
            last_id = int(last_id)
        except ValueError:
            return jsonify({'ok': False, 'error': 'cursor 格式错误'}), 400
        if sort == 'new':
            where.append('(date<? OR (date=? AND id<?))')
            args.extend([key, key, last_id])
        else:
            key = int(key)
            where.append('(relevance<? OR (relevance=? AND id<?))')
            args.extend([key, key, last_id])
    # 无 cursor 时不加日期过滤，默认查全部

    try:
        page_size = min(50, max(1, int(request.args.get('page_size', 10))))
    except ValueError:
        page_size = 10

    where_sql = ' AND '.join(where)
    where_clause = ('WHERE ' + where_sql) if where_sql else ''
    rows = database.query(
        'SELECT i.*, EXISTS(SELECT 1 FROM article_archive a WHERE a.intel_id=i.id) '
        'AS has_archive FROM intelligence i {} ORDER BY {} LIMIT {}'.format(
            where_clause, order_sql.replace(', ', ', i.'), page_size + 1), tuple(args))
    has_more = len(rows) > page_size
    rows = rows[:page_size]
    next_cursor = ''
    if has_more and rows:
        last = rows[-1]
        key = last['date'] if sort == 'new' else str(last['relevance'])
        next_cursor = '{}_{}'.format(key, last['id'])
    for r in rows:
        r['tags'] = json.loads(r['tags'] or '[]')
        # media: {'images': [...], 'videos': [{'url','type'}]}；老数据可能为空/非法 JSON
        try:
            m = json.loads(r.get('media') or '{}')
            if not isinstance(m, dict):
                m = {}
            r['media'] = {
                'images': [u for u in (m.get('images') or []) if isinstance(u, str)],
                'videos': [v for v in (m.get('videos') or [])
                           if isinstance(v, dict) and v.get('url')],
            }
        except (ValueError, TypeError):
            r['media'] = {'images': [], 'videos': []}

    # 首页才统计总数（按筛选条件，不限日期）
    total = 0
    if not cursor:
        total = database.query_one(
            'SELECT COUNT(*) AS c FROM intelligence ' + where_clause,
            tuple(args))['c']
    return jsonify({'ok': True, 'data': rows, 'total': total,
                    'next_cursor': next_cursor, 'has_more': has_more})


@app.route('/api/filters')
def api_filters():
    """获取所有可用的厂商/行业/标签 筛选选项（不限日期）"""
    rows = database.query(
        'SELECT vendor, industry, tags FROM intelligence')
    vendors, industries, tags = set(), set(), set()
    for r in rows:
        if r['vendor']:
            vendors.add(r['vendor'])
        if r['industry']:
            industries.add(r['industry'])
        for t in json.loads(r['tags'] or '[]'):
            tags.add(t)
    # 合并预置行业（即使当天无数据也展示）
    industries.update(config.INDUSTRIES)
    return jsonify({'ok': True, 'data': {
        'vendors': sorted(vendors),
        'industries': sorted(industries),
        'tags': sorted(tags),
    }})


@app.route('/api/dates')
def api_dates():
    """返回有数据的所有日期（倒序）"""
    rows = database.query('SELECT DISTINCT date FROM intelligence ORDER BY date DESC LIMIT 30')
    return jsonify({'ok': True, 'data': [r['date'] for r in rows]})


@app.route('/api/daily-focus')
def api_daily_focus():
    """每日关注：返回指定日期窗口的五维情报聚合。

    参数：
        date: 快照日期 YYYY-MM-DD（缺省今天）
        days: 窗口天数（缺省 config.DAILY_FOCUS_DAYS）
        refresh: 传 1 强制重新生成快照（后台手动刷新用，需鉴权）
    读取已缓存快照；无缓存则实时计算。
    """
    refresh = request.args.get('refresh') == '1'
    date_str = request.args.get('date') or ''
    days = None
    try:
        days = int(request.args.get('days', 0)) or None
    except ValueError:
        days = None

    # 强制刷新需管理员登录
    if refresh:
        if not session.get('admin'):
            return jsonify({'ok': False, 'error': '未授权'}), 401
        data = daily_focus.save_focus_snapshot(date_str, days)
        database.log('system', '手动刷新每日关注快照: {}'.format(data['date']))
        return jsonify({'ok': True, 'data': data})

    data = daily_focus.load_focus_snapshot(date_str)
    return jsonify({'ok': True, 'data': data})


@app.route('/api/daily-focus/dates')
def api_daily_focus_dates():
    """返回有每日关注快照的所有日期（倒序）"""
    rows = database.query(
        "SELECT key FROM config WHERE key LIKE 'daily_focus:%' ORDER BY key DESC")
    dates = [r['key'].split(':', 1)[1] for r in rows]
    return jsonify({'ok': True, 'data': dates})


# ==================== 热点统计看板 ====================
@app.route('/api/hot-stats')
def api_hot_stats():
    """数字孪生行业热点统计看板数据。

    参数：
        date: 快照日期 YYYY-MM-DD（缺省今天，读缓存快照；无快照则实时聚合）
        refresh: 1 强制重新生成快照（需管理员登录）
    """
    date_str = (request.args.get('date') or '').strip()
    refresh = request.args.get('refresh') == '1'
    if refresh:
        if not session.get('admin'):
            return jsonify({'ok': False, 'error': '未授权'}), 401
        data = hot_stats.save_snapshot(date_str or None)
        database.log('system', '手动刷新热点统计快照: {}'.format(data['date']))
        return jsonify({'ok': True, 'date': data['date'],
                        'generated_at': data.get('generated_at'), 'data': data})
    data = hot_stats.load_snapshot(date_str) if date_str else None
    if not data:
        # 今日尚无快照：实时聚合一份，并写入缓存（冷启动/首日场景）
        data = hot_stats.build_stats()
        try:
            database.set_config(hot_stats.cache_key(data['date']),
                                json.dumps(data, ensure_ascii=False))
        except Exception:
            pass
    return jsonify({'ok': True, 'date': data['date'],
                    'generated_at': data.get('generated_at'), 'data': data})


@app.route('/api/hot-stats/refresh', methods=['POST'])
def api_hot_stats_refresh():
    """手动触发当日热点统计快照刷新（公开接口，快照本身不含敏感数据）。"""
    try:
        data = hot_stats.save_snapshot()
        database.log('system', '手动刷新热点统计快照: {}'.format(data['date']))
        return jsonify({'ok': True, 'date': data['date'],
                        'generated_at': data.get('generated_at'), 'data': data})
    except Exception as exc:
        return jsonify({'ok': False, 'error': '刷新失败: {}'.format(exc)}), 500


@app.route('/api/hot-stats/dates')
def api_hot_stats_dates():
    """返回有热点统计快照的日期（倒序，最多30条）"""
    dates = hot_stats.list_snapshot_dates(limit=30)
    return jsonify({'ok': True, 'data': dates})


@app.route('/api/favorite/<int:iid>', methods=['POST'])
def api_favorite(iid):
    row = database.query_one('SELECT is_favorite FROM intelligence WHERE id=?', (iid,))
    if not row:
        return jsonify({'ok': False, 'error': '记录不存在'}), 404
    new_val = 0 if row['is_favorite'] else 1
    database.execute('UPDATE intelligence SET is_favorite=? WHERE id=?', (new_val, iid))
    return jsonify({'ok': True, 'data': {'is_favorite': new_val}})


# ==================== 后台 API ====================
@app.route('/api/admin/login', methods=['POST'])
def admin_login():
    data = request.get_json(silent=True) or {}
    pwd = data.get('password', '')
    if pwd == (config.ADMIN_PASSWORD or database.get_config('admin_password', 'luban2026')):
        session['admin'] = True
        return jsonify({'ok': True})
    return jsonify({'ok': False, 'error': '密码错误'})


@app.route('/api/admin/logout', methods=['POST'])
def admin_logout():
    session.pop('admin', None)
    return jsonify({'ok': True})


@app.route('/api/admin/status')
def admin_status():
    return jsonify({'ok': True, 'admin': bool(session.get('admin'))})


# -------- 大华功能导入 --------
@app.route('/api/admin/dahua', methods=['GET'])
@require_admin
def admin_dahua_list():
    rows = database.query('SELECT * FROM dahua_features ORDER BY id DESC')
    return jsonify({'ok': True, 'data': rows})


@app.route('/api/admin/dahua', methods=['POST'])
@require_admin
def admin_dahua_add():
    """添加单条大华功能"""
    data = request.get_json(silent=True) or {}
    name = (data.get('feature_name') or '').strip()
    if not name:
        return jsonify({'ok': False, 'error': '功能名称不能为空'}), 400
    iid = database.execute('''
        INSERT INTO dahua_features(category, feature_name, description, imported_at)
        VALUES (?,?,?,?)
    ''', (data.get('category', ''), name, data.get('description', ''),
          database.now_str()))
    database.backup_db()
    return jsonify({'ok': True, 'id': iid})


@app.route('/api/admin/dahua/<int:iid>', methods=['DELETE'])
@require_admin
def admin_dahua_delete(iid):
    database.execute('DELETE FROM dahua_features WHERE id=?', (iid,))
    database.backup_db()
    return jsonify({'ok': True})


@app.route('/api/admin/dahua/import-text', methods=['POST'])
@require_admin
def admin_dahua_import_text():
    """按文本导入：每行一条「功能名|描述|分类」，或纯文本每行一条"""
    data = request.get_json(silent=True) or {}
    text = data.get('text', '')
    category = data.get('category', '')
    count = 0
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = [p.strip() for p in re.split(r'[|｜\t]', line, maxsplit=2)]
        name = parts[0]
        desc = parts[1] if len(parts) > 1 else ''
        cat = parts[2] if len(parts) > 2 else category
        if not name:
            continue
        database.execute('''
            INSERT INTO dahua_features(category, feature_name, description, imported_at)
            VALUES (?,?,?,?)
        ''', (cat, name, desc, database.now_str()))
        count += 1
    database.backup_db()
    return jsonify({'ok': True, 'count': count})


# -------- 竞品分析 --------
def _build_analysis():
    """竞品分析核心逻辑：大华功能清单 vs 采集情报（供 API 与导出共用）"""
    features = database.query('SELECT * FROM dahua_features ORDER BY category, id')
    intels = database.query(
        'SELECT * FROM intelligence ORDER BY relevance DESC, id DESC LIMIT 300')

    vendor_caps = {}
    for it in intels:
        v = it['vendor'] or '行业动态'
        if v not in vendor_caps:
            vendor_caps[v] = []
        vendor_caps[v].append({
            'title': it['title'],
            'url': it['url'],
            'summary': (it['summary'] or '')[:150],
            'relevance': it['relevance'],
            'date': it['date'],
            'industry': it['industry'],
        })

    gaps = []
    for it in intels:
        text = (it['title'] + ' ' + it['summary']).lower()
        matched = False
        for f in features:
            kw = re.sub(r'[\s（()）【】\[\]：:、,，。.·]+', '', f['feature_name']).lower()
            if len(kw) >= 2 and kw in text.replace(' ', ''):
                matched = True
                break
        if not matched and it['relevance'] >= 3:
            gaps.append({
                'id': it['id'],
                'date': it['date'],
                'vendor': it['vendor'],
                'industry': it['industry'],
                'title': it['title'],
                'url': it['url'],
                'summary': (it['summary'] or '')[:200],
                'relevance': it['relevance'],
            })

    vendor_summary = []
    for v, items in sorted(vendor_caps.items(), key=lambda x: -len(x[1])):
        vendor_summary.append({'vendor': v, 'count': len(items),
                               'top': items[:3]})

    coverage = []
    for f in features:
        kw = re.sub(r'[\s（()）【】\[\]：:、,，。.·]+', '', f['feature_name']).lower()
        hits = [it for it in intels
                if len(kw) >= 2 and kw in (it['title'] + ' ' + it['summary']).replace(' ', '')]
        coverage.append({
            'category': f['category'],
            'feature': f['feature_name'],
            'industry_hits': len(hits),
            'recent_hits': [h['title'] for h in hits[:3]],
        })
    coverage.sort(key=lambda x: -x['industry_hits'])

    return {
        'feature_count': len(features),
        'intel_count': len(intels),
        'gaps': gaps,
        'vendor_summary': vendor_summary,
        'coverage': coverage,
    }


@app.route('/api/admin/analysis')
@require_admin
def admin_analysis():
    """基于大华功能清单 vs 采集情报，生成竞品差距报告"""
    return jsonify({'ok': True, 'data': _build_analysis()})


@app.route('/api/admin/analysis/export')
@require_admin
def admin_analysis_export():
    """导出自包含 HTML 竞品分析报告（浏览器直接下载）"""
    d = _build_analysis()
    now = database.now_str()
    rows_gap = ''.join(
        '<tr><td>{}</td><td>{}</td><td><a href="{}">{}</a></td><td>{}</td><td>{}</td></tr>'.format(
            g['date'], esc(g['vendor'] or '行业'), g['url'], esc(g['title']),
            g['relevance'], esc(g['summary']))
        for g in d['gaps'][:80])
    rows_cov = ''.join(
        '<tr><td>{}</td><td>{}</td><td style="text-align:center">{}</td><td>{}</td></tr>'.format(
            esc(c['category'] or '-'), esc(c['feature']), c['industry_hits'],
            esc('；'.join(c['recent_hits'][:2])))
        for c in d['coverage'][:60])
    rows_vend = ''.join(
        '<tr><td>{}</td><td style="text-align:center">{}</td></tr>'.format(esc(v['vendor']), v['count'])
        for v in d['vendor_summary'])
    html = '''<!DOCTYPE html><html lang="zh-CN"><head><meta charset="UTF-8">
<title>竞品分析报告 {now}</title>
<style>
body{{font-family:"PingFang SC","Microsoft YaHei",sans-serif;max-width:1000px;margin:24px auto;padding:0 16px;color:#24292f;line-height:1.7;}}
h1{{border-bottom:2px solid #58a6ff;padding-bottom:8px;}}
h2{{margin-top:28px;color:#0969da;}}
table{{border-collapse:collapse;width:100%;font-size:13px;margin:10px 0;}}
th,td{{border:1px solid #d0d7de;padding:6px 10px;text-align:left;vertical-align:top;}}
th{{background:#f6f8fa;}}
a{{color:#0969da;text-decoration:none;}}
.meta{{color:#57606a;font-size:13px;}}
.warn{{color:#cf222e;font-weight:600;}}
</style></head><body>
<h1>📡 行业情报雷达 · 竞品分析报告</h1>
<p class="meta">生成时间：{now} ｜ 大华功能 {fc} 条 ｜ 情报样本 {ic} 条 ｜ <span class="warn">潜在差距项 {gc} 条</span></p>
<h2>一、潜在差距项（友商有 / 大华清单未覆盖 · 相关度≥3）</h2>
<table><tr><th>日期</th><th>厂商</th><th>情报标题</th><th>相关度</th><th>摘要</th></tr>{rows_gap}</table>
<h2>二、厂商情报覆盖度</h2>
<table><tr><th>厂商</th><th>情报条数</th></tr>{rows_vend}</table>
<h2>三、大华功能行业热度（情报中出现越多 = 行业越热点）</h2>
<table><tr><th>分类</th><th>功能</th><th>命中次数</th><th>近期命中情报</th></tr>{rows_cov}</table>
<p class="meta">本报告由 intel-radar 自动生成</p>
</body></html>'''.format(now=now, fc=d['feature_count'], ic=d['intel_count'],
                         gc=len(d['gaps']), rows_gap=rows_gap,
                         rows_vend=rows_vend, rows_cov=rows_cov)
    return app.response_class(
        html, mimetype='text/html',
        headers={'Content-Disposition':
                 'attachment; filename="intel-radar-report-{}.html"'.format(
                     database.today_str())})


def esc(s):
    return (str(s or '').replace('&', '&amp;').replace('<', '&lt;')
            .replace('>', '&gt;').replace('"', '&quot;'))


# -------- 采集控制 --------
@app.route('/api/admin/collect', methods=['POST'])
@require_admin
def admin_collect():
    """异步启动采集：立即返回，后台线程执行，前端轮询 /api/admin/collect-status"""
    if collector.PROGRESS['running']:
        return jsonify({'ok': False, 'error': '采集正在进行中，请稍候'})
    # 冷却保护：距上次采集完成太近时拦截（重复采集因全局去重不会新增，
    # 且高频请求会触发搜索引擎风控，导致接下来几小时全部 0 结果）
    force = request.args.get('force') == '1'
    if not force and request.is_json:
        force = bool((request.json or {}).get('force'))
    finished_at = collector.PROGRESS.get('finished_at') or ''
    if not force and finished_at:
        try:
            from datetime import datetime
            last = datetime.strptime(finished_at, '%Y-%m-%d %H:%M:%S')
            elapsed_h = (datetime.now() - last).total_seconds() / 3600.0
            cooldown = float(config.MANUAL_COOLDOWN_HOURS)
            if 0 <= elapsed_h < cooldown:
                return jsonify({'ok': False, 'error': (
                    '距上次采集完成仅 {:.1f} 小时（冷却期 {} 小时）。'
                    '同一批文章已按标题/URL 全局去重，重复采集不会新增；'
                    '频繁采集还会触发搜索引擎风控。确有必要请点「强制采集」。'
                ).format(elapsed_h, cooldown)})
        except (ValueError, TypeError):
            pass
    t = threading.Thread(target=_run_collect, daemon=True)
    t.start()
    return jsonify({'ok': True, 'msg': '采集已启动', 'total': collector.PROGRESS['total']})


def _run_collect():
    try:
        collector.collect_once()
    except Exception as e:
        collector.PROGRESS['running'] = False
        database.log('collect', '采集线程异常: {}'.format(e), 'error')
    finally:
        database.backup_db(force=True)


# -------- 数据整理（存量摘要升级 + 垃圾清理） --------
REPROCESS = {'running': False, 'total': 0, 'done': 0, 'updated': 0,
             'deleted': 0, 'failed': 0, 'finished_at': '', 'log': []}


def _run_reprocess():
    """后台线程：清理无正文/超龄旧文，对存量短摘要(<100字)重跑 AI 速览；低分条目直接清理"""
    st = REPROCESS
    st.update(running=True, total=0, done=0, updated=0, deleted=0,
              failed=0, finished_at='', log=[])
    try:
        # 第一步：清理无正文垃圾（导航页/聚合页/空壳页）
        rows = database.query(
            "SELECT id, title FROM intelligence WHERE length(coalesce(description,'')) < 60")
        for r in rows:
            database.delete_intelligence(r['id'])
            st['deleted'] += 1
        st['log'].append('清理无正文条目: {} 条'.format(len(rows)))

        # 第二步：清理超龄旧文（能解析出发布时间且超过 FRESH_DAYS 天的）
        rows = database.query('SELECT id, title, description, published FROM intelligence')
        stale = []
        for r in rows:
            pub = r['published'] or collector._norm_date(
                collector._extract_date((r['title'] or '') + ' ' + (r['description'] or '')))
            if pub and not r['published']:
                database.execute('UPDATE intelligence SET published=? WHERE id=?',
                                 (pub, r['id']))
            if pub and collector._is_stale(pub):
                stale.append(r['id'])
        for rid in stale:
            database.delete_intelligence(rid)
            st['deleted'] += 1
        st['log'].append('清理超龄旧文(发布>{}天): {} 条'.format(config.FRESH_DAYS, len(stale)))

        # 第三步：回填原文图片/视频（存量条目 media 为空时抓文章页提取，限额控制耗时）
        rows = database.query(
            "SELECT id, url, image FROM intelligence "
            "WHERE (media IS NULL OR media='') AND url LIKE 'http%' "
            "ORDER BY id DESC LIMIT 30")
        st['log'].append('媒体回填: 待处理 {} 条'.format(len(rows)))
        media_added = 0
        for r in rows:
            it = {'url': r['url'], 'image': r['image'] or ''}
            collector._enrich_article_media(it)
            if it.get('media') and (it['media'].get('images') or it['media'].get('videos')):
                database.execute(
                    'UPDATE intelligence SET image=?, media=? WHERE id=?',
                    (it.get('image', r['image'] or ''),
                     json.dumps(it['media'], ensure_ascii=False), r['id']))
                st['updated'] += 1
                media_added += 1
            elif it.get('image') and not r['image']:
                # 没抓到媒体但补到了缩略图，也写回
                database.execute('UPDATE intelligence SET image=? WHERE id=?',
                                 (it['image'], r['id']))
        st['log'].append('媒体回填完成: 补充 {} 条'.format(media_added))

        # 第四步：AI 重跑短摘要
        rows = database.query(
            'SELECT id, title, summary, description, vendor, tags '
            'FROM intelligence WHERE length(summary) < 100 ORDER BY id')
        st['total'] = len(rows)
        if rows and not ai.enabled():
            st['log'].append('未配置 AI_API_KEY，跳过摘要升级')
            rows = []
        batch = 15
        for start in range(0, len(rows), batch):
            chunk = rows[start:start + batch]
            items = []
            for r in chunk:
                text = r['description'] or ''
                if len(text) < len(r['summary'] or ''):
                    text = r['summary'] or ''
                items.append({'title': r['title'], 'summary': text,
                              'vendor': r['vendor']})
            res = ai.analyze_batch(items)
            if res is None:
                st['failed'] += len(chunk)
                st['done'] += len(chunk)
                continue
            for i, r in enumerate(chunk):
                x = res[i] or {}
                score = x.get('score', 3)
                if (not x.get('keep', True)) or score < config.AI_MIN_SCORE:
                    database.delete_intelligence(r['id'])
                    st['deleted'] += 1
                else:
                    new_sum = (x.get('summary') or '').strip() or r['summary']
                    new_tags = x.get('tags') or json.loads(r['tags'] or '[]')
                    database.execute(
                        'UPDATE intelligence SET summary=?, tags=?, relevance=? '
                        'WHERE id=?',
                        (new_sum, json.dumps(new_tags, ensure_ascii=False),
                         score, r['id']))
                    st['updated'] += 1
                st['done'] += 1
            st['log'].append('已处理 {}/{}'.format(st['done'], st['total']))
        database.log('collect', '数据整理完成: 升级{}条 清理{}条'.format(
            st['updated'], st['deleted']))
        st['log'].append('完成：升级 {} 条，清理 {} 条'.format(
            st['updated'], st['deleted']))
    except Exception as e:
        st['log'].append('出错: {}'.format(e))
        database.log('collect', '数据整理异常: {}'.format(e), 'warn')
    finally:
        st['running'] = False
        st['finished_at'] = database.now_str()
        database.backup_db(force=True)


@app.route('/api/admin/reprocess', methods=['POST'])
@require_admin
def admin_reprocess():
    """启动数据整理（后台线程），前端轮询 /api/admin/reprocess-status"""
    if REPROCESS['running']:
        return jsonify({'ok': False, 'error': '数据整理正在进行中'})
    if collector.PROGRESS['running']:
        return jsonify({'ok': False, 'error': '采集正在进行中，请等采集结束后再整理'})
    if MEDIA_RESCAN['running']:
        return jsonify({'ok': False, 'error': '媒体重抓正在进行中，请等重抓结束后再整理'})
    t = threading.Thread(target=_run_reprocess, daemon=True)
    t.start()
    return jsonify({'ok': True, 'msg': '数据整理已启动'})


@app.route('/api/admin/reprocess-status')
@require_admin
def admin_reprocess_status():
    """前端轮询：返回内存里的进度（running/done/total）+ 上次完成的真实数字（来自数据库日志，避免前后端状态不一致）"""
    data = dict(REPROCESS)
    # 从数据库 collect_log 读最近一条「数据整理完成」记录作为权威结果
    # 这样即便 Flask worker 被 Render 回收内存状态丢失，前端仍能看到真实完成数
    last = database.query_one(
        "SELECT detail, created_at FROM collect_log "
        "WHERE action='collect' AND detail LIKE '数据整理完成%' "
        "ORDER BY id DESC LIMIT 1")
    if last:
        data['last_finished'] = {
            'detail': last['detail'],
            'ts': last['created_at'],
        }
    return jsonify({'ok': True, 'data': data})


# -------- 强制重抓媒体（全量 media 重刷） --------
MEDIA_RESCAN = {'running': False, 'total': 0, 'done': 0, 'updated': 0,
                'kept': 0, 'failed': 0, 'finished_at': '', 'log': []}


@app.route('/api/admin/media-rescan', methods=['POST'])
@require_admin
def admin_media_rescan():
    """启动强制重抓媒体（后台线程）：对所有带链接条目重新提取图片/视频。

    与「数据整理」第三步的区别：数据整理只回填 media 为空的条目；
    本功能全量重刷（含已有 media 的），用于媒体提取逻辑升级后重刷存量数据。
    新提取有内容则替换旧 media；提取为空则保留旧值（防瞬时失败误删好数据）。
    """
    if MEDIA_RESCAN['running']:
        return jsonify({'ok': False, 'error': '媒体重抓正在进行中'})
    if collector.PROGRESS['running']:
        return jsonify({'ok': False, 'error': '采集正在进行中，请等采集结束后再重抓'})
    if REPROCESS['running']:
        return jsonify({'ok': False, 'error': '数据整理正在进行中，请等整理结束后再重抓'})
    t = threading.Thread(target=_run_media_rescan, daemon=True)
    t.start()
    return jsonify({'ok': True, 'msg': '媒体重抓已启动'})


def _run_media_rescan():
    """后台线程：全量重抓媒体（图片/视频/缩略图），进度写 MEDIA_RESCAN。"""
    st = MEDIA_RESCAN
    st.update(running=True, total=0, done=0, updated=0, kept=0,
              failed=0, finished_at='', log=[])
    try:
        rows = database.query(
            "SELECT id, url, image FROM intelligence "
            "WHERE url LIKE 'http%' ORDER BY id DESC LIMIT 200")
        st['total'] = len(rows)
        st['log'].append('全量媒体重抓: 待处理 {} 条'.format(len(rows)))
        for r in rows:
            it = {'url': r['url'], 'image': r['image'] or ''}
            collector._enrich_article_media(it)
            new_media = it.get('media') or {}
            has_new = bool(new_media.get('images') or new_media.get('videos'))
            if has_new:
                # 新提取有内容：替换旧 media（重刷的意义所在——应用最新提取/过滤逻辑）
                database.execute(
                    'UPDATE intelligence SET image=?, media=? WHERE id=?',
                    (it.get('image') or r['image'] or '',
                     json.dumps(new_media, ensure_ascii=False), r['id']))
                st['updated'] += 1
            elif it.get('image') and it.get('image') != (r['image'] or ''):
                # 只补到缩略图没抓到正文媒体：仅更新 image，不动旧 media
                database.execute('UPDATE intelligence SET image=? WHERE id=?',
                                 (it['image'], r['id']))
                st['kept'] += 1
            else:
                # 提取为空（反爬/源站异常）：保留旧值防误删
                st['kept'] += 1
            st['done'] += 1
        database.log('collect', '媒体重抓完成: 更新{}条 保留{}条'.format(
            st['updated'], st['kept']))
        st['log'].append('完成：更新 {} 条，保留 {} 条'.format(
            st['updated'], st['kept']))
    except Exception as e:
        st['log'].append('出错: {}'.format(e))
        database.log('collect', '媒体重抓异常: {}'.format(e), 'warn')
    finally:
        st['running'] = False
        st['finished_at'] = database.now_str()
        database.backup_db(force=True)


@app.route('/api/admin/media-rescan-status')
@require_admin
def admin_media_rescan_status():
    """前端轮询：媒体重抓进度 + 数据库日志里的上次完成记录（权威源）"""
    data = dict(MEDIA_RESCAN)
    last = database.query_one(
        "SELECT detail, created_at FROM collect_log "
        "WHERE action='collect' AND detail LIKE '媒体重抓完成%' "
        "ORDER BY id DESC LIMIT 1")
    if last:
        data['last_finished'] = {
            'detail': last['detail'],
            'ts': last['created_at'],
        }
    return jsonify({'ok': True, 'data': data})


@app.route('/api/admin/collect-status')
@require_admin
def admin_collect_status():
    return jsonify({'ok': True, 'data': collector.collect_status()})


@app.route('/api/admin/ai-status')
@require_admin
def admin_ai_status():
    """AI 提炼模块状态与连通性检查"""
    import ai as ai_mod
    info = {
        'enabled': ai_mod.enabled(),
        'model': ai_mod.MODEL,
        'api_base': ai_mod.API_BASE,
        'min_score': config.AI_MIN_SCORE,
    }
    if ai_mod.enabled():
        info.update(ai_mod.test_connection())
    else:
        info['ok'] = False
        info['msg'] = '未配置 AI_API_KEY（当前为关键词降级模式）'
    return jsonify({'ok': True, 'data': info})


@app.route('/api/admin/diagnose', methods=['POST'])
@require_admin
def admin_diagnose():
    """诊断单个查询：返回百度/必应/搜狗各源原始结果数（不写入数据库）"""
    data = request.get_json(silent=True) or {}
    query = (data.get('query') or '海康威视 数字孪生').strip()
    result = collector.diagnose(query)
    return jsonify({'ok': True, 'data': result})


@app.route('/api/admin/push-test', methods=['POST'])
@require_admin
def admin_push_test():
    ok, msg = pusher.push_manual()
    database.backup_db()
    return jsonify({'ok': ok, 'msg': msg})


@app.route('/api/admin/push-now', methods=['POST'])
@require_admin
def admin_push_now():
    ok, msg = pusher.push_daily_top()
    database.backup_db()
    return jsonify({'ok': ok, 'msg': msg})


# -------- 设置 --------
@app.route('/api/admin/settings', methods=['GET'])
@require_admin
def admin_settings_get():
    return jsonify({'ok': True, 'data': {
        'pushplus_token': database.get_config('pushplus_token', ''),
        'coll_time': database.get_config('coll_time', config.COLLECT_TIME),
        'push_time': database.get_config('push_time', config.PUSH_TIME),
        'push_top_n': int(database.get_config('push_top_n', str(config.PUSH_TOP_N))),
        'intel_retention_days': int(database.get_config(
            'intel_retention_days', str(config.INTEL_RETENTION_DAYS))),
        'admin_password_configured': bool(config.ADMIN_PASSWORD),
    }})


@app.route('/api/admin/settings', methods=['POST'])
@require_admin
def admin_settings_save():
    data = request.get_json(silent=True) or {}
    for key in ('pushplus_token', 'coll_time', 'push_time', 'push_top_n'):
        if key in data:
            database.set_config(key, data[key])
    if 'intel_retention_days' in data:
        try:
            days = int(data['intel_retention_days'])
        except (TypeError, ValueError):
            days = config.INTEL_RETENTION_DAYS
        # 7~3650 天；0/负数视为永不过期，统一夹到安全区间
        days = 0 if days <= 0 else max(7, min(3650, days))
        database.set_config('intel_retention_days', str(days))
    database.backup_db(force=True)
    # 热更新调度
    _reschedule()
    return jsonify({'ok': True})


@app.route('/api/admin/logs')
@require_admin
def admin_logs():
    try:
        rows = database.query('SELECT * FROM collect_log ORDER BY id DESC LIMIT 100')
        return jsonify({'ok': True, 'data': rows})
    except Exception as e:
        database.log('system', '读取日志失败: {}'.format(e), 'error')
        return jsonify({'ok': False, 'error': '读取日志失败: {}'.format(e)}), 500


# -------- 隔离区（低质条目审查） --------
@app.route('/api/admin/quarantine')
@require_admin
def admin_quarantine_list():
    """隔离区清单 + 按原因分类统计（被质量门禁拦截的原始数据备查）"""
    try:
        rows = database.query(
            'SELECT id, created_at, vendor, title, source, url, reason, note, origin '
            'FROM quarantine ORDER BY id DESC LIMIT 200')
        return jsonify({'ok': True, 'data': rows,
                        'stats': database.quarantine_stats()})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500


@app.route('/api/admin/quarantine/<int:qid>')
@require_admin
def admin_quarantine_detail(qid):
    """单条隔离记录全量字段（含原始摘要/描述与质量指标）"""
    row = database.query_one('SELECT * FROM quarantine WHERE id=?', (qid,))
    if not row:
        return jsonify({'ok': False, 'error': '记录不存在'}), 404
    return jsonify({'ok': True, 'data': row})


@app.route('/api/admin/quarantine/scan', methods=['POST'])
@require_admin
def admin_quarantine_scan():
    """审计存量情报：按与采集门禁一致的规则复检，低质条目移入隔离区（原数据备查）。
    参数 days：只审计近 N 天（默认 90）；dry_run=1 只报告不动库。"""
    data = request.get_json(silent=True) or {}
    days = max(1, min(3650, int(data.get('days', 90) or 90)))
    dry_run = bool(data.get('dry_run'))
    cut = (datetime.now() - timedelta(days=days)).strftime('%Y-%m-%d')
    rows = database.query('SELECT * FROM intelligence WHERE date>=?', (cut,))
    moved = []
    cleaned = 0
    for r in rows:
        arch = database.get_archive(r['id'])
        metrics = None
        if arch:
            metrics = collector._page_quality_metrics(arch.get('html'), arch.get('plain_text'))
        snippet = (r.get('summary') or '') + ' ' + (r.get('description') or '')
        reason, note = collector.quality_verdict(
            url=r.get('url'), title=r.get('title'),
            snippet=snippet, metrics=metrics)
        if not reason:
            # 留存条目顺手回填清洗：剥离摘要/描述里搜索引擎截断的残缺 URL
            new_sum = collector._strip_truncated_urls(r.get('summary') or '')
            new_desc = collector._strip_truncated_urls(r.get('description') or '')
            if new_sum != (r.get('summary') or '') or new_desc != (r.get('description') or ''):
                database.execute('UPDATE intelligence SET summary=?, description=? WHERE id=?',
                                 (new_sum, new_desc, r['id']))
                cleaned += 1
            continue
        moved.append({'id': r['id'], 'title': r['title'], 'url': r.get('url'),
                      'reason': reason, 'note': note,
                      'metrics': metrics or {'eff_snippet':
                                             collector._effective_snippet_len(snippet)}})
        if dry_run:
            continue
        if database.add_quarantine(r, reason, note, metrics=metrics or {},
                                   origin='scan', exclude_intel_id=r['id']):
            database.delete_intelligence(r['id'])
        else:
            moved[-1]['note'] += '（隔离区写入失败：标题/URL 已存在，仅保留情报）'
    if not dry_run and (moved or cleaned):
        database.log('collect', '存量质量扫描: 隔离 {} 条 / 清洗描述 {} 条（近{}天）'.format(
            len(moved), cleaned, days), 'ok')
        database.backup_db(force=True)
    return jsonify({'ok': True, 'scanned': len(rows), 'quarantined': len(moved),
                    'cleaned': cleaned, 'dry_run': dry_run, 'data': moved})


@app.route('/api/admin/quarantine/promote', methods=['POST'])
@require_admin
def admin_quarantine_promote():
    """深抓复检：重新抓取隔离条目的 URL，按质量门禁复判。
    通过 → 入库（含全文存档+媒体）；仍不通过 → 返回原因（人审后可 DELETE 放弃）。"""
    data = request.get_json(silent=True) or {}
    qid = int(data.get('id', 0) or 0)
    q = database.query_one('SELECT * FROM quarantine WHERE id=?', (qid,))
    if not q:
        return jsonify({'ok': False, 'error': '隔离记录不存在'}), 404
    url = q.get('url') or ''
    if not url.startswith('http'):
        return jsonify({'ok': False, 'error': '无有效 URL，无法深抓'}), 400

    item = {'title': q['title'], 'url': url, 'source': q.get('source') or '',
            'summary': q.get('summary') or '', 'description': q.get('description') or '',
            'published': q.get('published') or '', 'vendor': q.get('vendor') or ''}
    collector._enrich_article_media(item)
    metrics = item.get('_quality')
    reason, note = collector.quality_verdict(url=url, title=q['title'],
                                              snippet=(q.get('summary') or '') + ' ' + (q.get('description') or ''),
                                              metrics=metrics)
    if reason:
        return jsonify({'ok': False, 'reason': reason, 'note': note,
                        'metrics': metrics}), 200

    rel = collector.score_relevance(q['title'], q.get('summary') or '')
    new_id = database.add_intelligence({
        'date': database.today_str(), 'vendor': q.get('vendor') or '',
        'industry': collector.detect_industry(q['title'], q.get('summary') or ''),
        'title': q['title'], 'source': q.get('source') or '', 'url': url,
        'summary': q.get('summary') or '', 'description': q.get('description') or '',
        'image': item.get('image', ''), 'media': item.get('media') or {},
        'relevance': max(rel, 3), 'tags': collector.detect_tags(q['title'], q.get('summary') or ''),
        'published': q.get('published') or '',
    })
    if not new_id:
        return jsonify({'ok': False, 'error': '标题/URL 已存在于情报库'}), 409
    arch = item.get('_arch')
    if arch:
        database.save_archive(new_id, url, arch.get('base', ''),
                              arch.get('html', ''), arch.get('text', ''))
    database.execute('DELETE FROM quarantine WHERE id=?', (qid,))
    database.backup_db(force=True)
    database.log('collect', '隔离条目深抓复检通过，恢复入库: {}'.format(q['title'][:40]), 'ok')
    return jsonify({'ok': True, 'intel_id': new_id, 'metrics': metrics})


@app.route('/api/admin/quarantine/<int:qid>', methods=['DELETE'])
@require_admin
def admin_quarantine_delete(qid):
    """放弃一条隔离条目（人审后确认无价值，从备查区移除）"""
    database.execute('DELETE FROM quarantine WHERE id=?', (qid,))
    database.backup_db()
    return jsonify({'ok': True})


# ==================== 调度器 ====================
scheduler = BackgroundScheduler(timezone='Asia/Shanghai')


def _reschedule():
    """按设置重建每日任务（采集+推送）"""
    for job in list(scheduler.get_jobs()):
        job.remove()
    coll_time = database.get_config('coll_time', config.COLLECT_TIME)
    push_time = database.get_config('push_time', config.PUSH_TIME)
    try:
        ch, cm = map(int, coll_time.split(':'))
        scheduler.add_job(job_collect, CronTrigger(hour=ch, minute=cm),
                          id='daily_collect', replace_existing=True)
    except Exception:
        pass
    try:
        ph, pm = map(int, push_time.split(':'))
        scheduler.add_job(job_push, CronTrigger(hour=ph, minute=pm),
                          id='daily_push', replace_existing=True)
    except Exception:
        pass


def _job_lock_acquire(name, ttl_seconds):
    """跨实例任务锁（存 config 表，云端共享 PostgreSQL）。

    背景：CloudBase 滚动部署时新老实例可能同时存活，各自的 apscheduler
    都会触发 job_collect/job_push，导致重复采集/重复推送。
    本锁以数据库为仲裁：同一时刻只有一个实例能拿到令牌。
    成功返回 token；锁被其他实例占用返回 None；
    锁机制本身故障时返回 'fallback'（不因锁故障阻塞任务）。
    """
    token = '{}-{}'.format(int(time.time()), uuid.uuid4().hex[:8])
    key = 'lock:{}'.format(name)
    try:
        if database.PG:
            database.execute(
                'INSERT INTO config(key,value) VALUES(%s,%s) '
                'ON CONFLICT(key) DO NOTHING', (key, token))
        else:
            database.execute(
                'INSERT OR IGNORE INTO config(key,value) VALUES(?,?)',
                (key, token))
        row = database.query_one(
            'SELECT value FROM config WHERE key=' + database.PH, (key,))
        val = (row or {}).get('value', '')
        if val == token:
            return token
        # 锁已存在：判断是否过期（持有者崩溃未释放时允许接管）
        try:
            held_ts = int(str(val).split('-')[0])
        except Exception:
            held_ts = 0
        if int(time.time()) - held_ts > ttl_seconds:
            database.execute(
                'UPDATE config SET value=' + database.PH +
                ' WHERE key=' + database.PH, (token, key))
            row = database.query_one(
                'SELECT value FROM config WHERE key=' + database.PH, (key,))
            if (row or {}).get('value', '') == token:
                return token
        return None
    except Exception:
        return 'fallback'


def _job_lock_release(name, token):
    if not token or token == 'fallback':
        return
    try:
        database.execute(
            'DELETE FROM config WHERE key=' + database.PH +
            ' AND value=' + database.PH,
            ('lock:{}'.format(name), token))
    except Exception:
        pass


def job_collect():
    token = _job_lock_acquire('daily_collect', ttl_seconds=3600)
    if token is None:
        database.log('system', '检测到另一实例正在执行采集，本轮跳过（防重跑锁）')
        print('[{}] 采集被防重跑锁拦截'.format(database.now_str()))
        return
    try:
        added = collector.collect_once()
        print('[{}] 每日采集完成，新增 {} 条'.format(database.now_str(), added))
    except Exception as e:
        print('[{}] 采集失败: {}'.format(database.now_str(), e))
    finally:
        _job_lock_release('daily_collect', token)

    # 采集完成后生成「每日关注」当日快照（复用采集锁令牌，避免多实例重复写）
    # 若采集本身失败也尝试生成，保证当日概览可用（快照内容来自已有数据）
    if token is not None:
        try:
            d, ts = daily_focus.generate_today()
            database.log('system', '每日关注快照已生成: {} @ {}'.format(d, ts))
        except Exception as e:
            database.log('system', '每日关注快照生成失败: {}'.format(e), status='error')
        try:
            d, ts = hot_stats.generate_today()
            database.log('system', '热点统计快照已生成: {} @ {}'.format(d, ts))
        except Exception as e:
            database.log('system', '热点统计快照生成失败: {}'.format(e), status='error')


def job_push():
    token = _job_lock_acquire('daily_push', ttl_seconds=600)
    if token is None:
        database.log('system', '检测到另一实例正在推送，本轮跳过（防重跑锁）')
        print('[{}] 推送被防重跑锁拦截'.format(database.now_str()))
        return
    try:
        ok, msg = pusher.push_daily_top()
        print('[{}] 每日推送: {}'.format(database.now_str(), msg))
    except Exception as e:
        print('[{}] 推送失败: {}'.format(database.now_str(), e))
    finally:
        _job_lock_release('daily_push', token)


if not scheduler.running:
    _reschedule()
    scheduler.start()


# ==================== 启动 ====================
if __name__ == '__main__':
    print('=' * 50)
    print('行业情报雷达启动中...')
    print('  访问地址: http://{}:{}'.format(config.HOST, config.PORT))
    print('  后台入口: http://{}:{}/#admin'.format(config.HOST, config.PORT))
    print('  采集时间: {} | 推送时间: {}'.format(
        database.get_config('coll_time', config.COLLECT_TIME),
        database.get_config('push_time', config.PUSH_TIME)))
    print('=' * 50)
    app.run(host=config.HOST, port=config.PORT, debug=config.DEBUG, use_reloader=False)
