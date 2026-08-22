# -*- coding: utf-8 -*-
"""行业情报雷达 - Flask 主应用
包含：公开页API / 后台管理API / 竞品分析引擎 / 每日调度
部署：gunicorn app:app 或 python app.py
"""
import json
import os
import re
import threading
from datetime import datetime, date
from functools import wraps

from flask import (Flask, render_template, request, jsonify, session,
                   send_from_directory)
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

import config
import database
import collector
import pusher
import ai

app = Flask(__name__)
app.secret_key = config.SECRET_KEY
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024  # 16MB上传限制

# ==================== 初始化 ====================
database.init_db()

# 首次启动把默认配置写入数据库
# 当代码升级（config_version 变化）时，重新同步时间/条数默认值
CONFIG_VERSION = '2'
if database.get_config('config_version') != CONFIG_VERSION:
    database.set_config('config_version', CONFIG_VERSION)
    database.set_config('vendors_configured', '1')
    database.set_config('coll_time', config.COLLECT_TIME)
    database.set_config('push_time', config.PUSH_TIME)
    database.set_config('push_top_n', str(config.PUSH_TOP_N))


# ==================== 鉴权 ====================
def require_admin(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not session.get('admin'):
            return jsonify({'ok': False, 'error': '未登录'}), 401
        return f(*args, **kwargs)
    return wrapper


# ==================== 页面 ====================
@app.route('/')
def index():
    return render_template('index.html')


@app.route('/favicon.ico')
def favicon():
    return '', 204


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

    - 首次请求（无 cursor）：按 date 过滤（默认今天）返回第一页 + next_cursor
    - 带 cursor 请求：自动去掉日期过滤，跨日期加载更早记录（滚动增量加载）：
        sort=new → 按 (date, id) 游标，新→旧
        sort=rel → 按 (relevance, id) 游标，相关度高→低
    """
    where, args = [], []
    d = request.args.get('date') or database.today_str()
    vendor = request.args.get('vendor', '')
    industry = request.args.get('industry', '')
    tag = request.args.get('tag', '')
    relevance = request.args.get('relevance', '')
    fav = request.args.get('fav', '')
    keyword = request.args.get('q', '')
    cursor = request.args.get('cursor', '')
    sort = request.args.get('sort', 'rel')

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

    if sort == 'new':
        order_sql = 'date DESC, id DESC'
    else:
        sort = 'rel'
        order_sql = 'relevance DESC, id DESC'

    if cursor:
        # 滚动加载：去掉日期过滤，按游标跨日期取更早/更低优先级的记录
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
    else:
        where.append('date=?')
        args.append(d)

    try:
        page_size = min(50, max(1, int(request.args.get('page_size', 8))))
    except ValueError:
        page_size = 8

    where_sql = ' AND '.join(where)
    rows = database.query(
        'SELECT * FROM intelligence WHERE {} ORDER BY {} LIMIT {}'.format(
            where_sql, order_sql, page_size + 1), tuple(args))
    has_more = len(rows) > page_size
    rows = rows[:page_size]
    next_cursor = ''
    if has_more and rows:
        last = rows[-1]
        key = last['date'] if sort == 'new' else str(last['relevance'])
        next_cursor = '{}_{}'.format(key, last['id'])
    for r in rows:
        r['tags'] = json.loads(r['tags'] or '[]')

    # 首页才统计总数（按当日 + 筛选条件；此时 where 末位即 date=?，参数顺序一致）
    total = 0
    if not cursor:
        total = database.query_one(
            'SELECT COUNT(*) AS c FROM intelligence WHERE ' + where_sql,
            tuple(args))['c']
    return jsonify({'ok': True, 'data': rows, 'total': total,
                    'next_cursor': next_cursor, 'has_more': has_more})


@app.route('/api/filters')
def api_filters():
    """获取所有可用的厂商/行业/标签 筛选选项"""
    rows = database.query(
        'SELECT vendor, industry, tags FROM intelligence WHERE date=?', (database.today_str(),))
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
        database.backup_db()


# -------- 数据整理（存量摘要升级 + 垃圾清理） --------
REPROCESS = {'running': False, 'total': 0, 'done': 0, 'updated': 0,
             'deleted': 0, 'failed': 0, 'finished_at': '', 'log': []}


def _run_reprocess():
    """后台线程：对存量短摘要(<100字)重跑 AI 速览；无正文/低分条目直接清理"""
    st = REPROCESS
    st.update(running=True, total=0, done=0, updated=0, deleted=0,
              failed=0, finished_at='', log=[])
    try:
        # 第一步：清理无正文垃圾（导航页/聚合页/空壳页）
        rows = database.query(
            "SELECT id, title FROM intelligence WHERE length(coalesce(description,'')) < 60")
        for r in rows:
            database.execute('DELETE FROM intelligence WHERE id=?', (r['id'],))
            st['deleted'] += 1
        st['log'].append('清理无正文条目: {} 条'.format(len(rows)))

        # 第二步：AI 重跑短摘要
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
                    database.execute('DELETE FROM intelligence WHERE id=?',
                                     (r['id'],))
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


@app.route('/api/admin/reprocess', methods=['POST'])
@require_admin
def admin_reprocess():
    """启动数据整理（后台线程），前端轮询 /api/admin/reprocess-status"""
    if REPROCESS['running']:
        return jsonify({'ok': False, 'error': '数据整理正在进行中'})
    if collector.PROGRESS['running']:
        return jsonify({'ok': False, 'error': '采集正在进行中，请等采集结束后再整理'})
    t = threading.Thread(target=_run_reprocess, daemon=True)
    t.start()
    return jsonify({'ok': True, 'msg': '数据整理已启动'})


@app.route('/api/admin/reprocess-status')
@require_admin
def admin_reprocess_status():
    return jsonify({'ok': True, 'data': REPROCESS})


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
        'admin_password_configured': bool(config.ADMIN_PASSWORD),
    }})


@app.route('/api/admin/settings', methods=['POST'])
@require_admin
def admin_settings_save():
    data = request.get_json(silent=True) or {}
    for key in ('pushplus_token', 'coll_time', 'push_time', 'push_top_n'):
        if key in data:
            database.set_config(key, data[key])
    database.backup_db()
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


def job_collect():
    try:
        added = collector.collect_once()
        print('[{}] 每日采集完成，新增 {} 条'.format(database.now_str(), added))
    except Exception as e:
        print('[{}] 采集失败: {}'.format(database.now_str(), e))


def job_push():
    try:
        ok, msg = pusher.push_daily_top()
        print('[{}] 每日推送: {}'.format(database.now_str(), msg))
    except Exception as e:
        print('[{}] 推送失败: {}'.format(database.now_str(), e))


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
