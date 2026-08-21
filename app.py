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


@app.route('/api/intelligence')
def api_intelligence():
    """情报列表，支持 date/vendor/industry/tag/relevance/fav 过滤"""
    where, args = [], []
    d = request.args.get('date') or database.today_str()
    vendor = request.args.get('vendor', '')
    industry = request.args.get('industry', '')
    tag = request.args.get('tag', '')
    relevance = request.args.get('relevance', '')
    fav = request.args.get('fav', '')
    keyword = request.args.get('q', '')

    where.append('date=?')
    args.append(d)
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

    rows = database.query(
        'SELECT * FROM intelligence WHERE {} ORDER BY relevance DESC, id DESC LIMIT 200'
        .format(' AND '.join(where)), tuple(args))
    for r in rows:
        r['tags'] = json.loads(r['tags'] or '[]')
    return jsonify({'ok': True, 'data': rows})


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
@app.route('/api/admin/analysis')
@require_admin
def admin_analysis():
    """基于大华功能清单 vs 采集情报，生成竞品差距报告"""
    features = database.query('SELECT * FROM dahua_features ORDER BY category, id')
    intels = database.query(
        'SELECT * FROM intelligence ORDER BY relevance DESC, id DESC LIMIT 300')

    # 1) 情报中出现的厂商能力（去重聚合）
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

    # 2) 大华功能 vs 情报关键词匹配：找出"友商有、大华无"的差距
    gaps = []
    for it in intels:
        text = (it['title'] + ' ' + it['summary']).lower()
        matched = False
        for f in features:
            # 用功能名里的核心词做匹配（去掉常见修饰词）
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

    # 3) 按厂商汇总
    vendor_summary = []
    for v, items in sorted(vendor_caps.items(), key=lambda x: -len(x[1])):
        vendor_summary.append({'vendor': v, 'count': len(items),
                               'top': items[:3]})

    # 4) 大华功能覆盖分析（哪些功能在情报中频繁出现 = 行业热点）
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

    return jsonify({'ok': True, 'data': {
        'feature_count': len(features),
        'intel_count': len(intels),
        'gaps': gaps,
        'vendor_summary': vendor_summary,
        'coverage': coverage,
    }})


# -------- 采集控制 --------
@app.route('/api/admin/collect', methods=['POST'])
@require_admin
def admin_collect():
    """异步启动采集：立即返回，后台线程执行，前端轮询 /api/admin/collect-status"""
    if collector.PROGRESS['running']:
        return jsonify({'ok': False, 'error': '采集正在进行中，请稍候'})
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


@app.route('/api/admin/collect-status')
@require_admin
def admin_collect_status():
    return jsonify({'ok': True, 'data': collector.collect_status()})


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
        rows = database.query('SELECT * FROM collect_log ORDER BY id DESC LIMIT 30')
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
