# -*- coding: utf-8 -*-
"""行业情报雷达 - 数据层（兼容 SQLite 本地 / PostgreSQL 云端）
本地默认 SQLite；若设置了环境变量 DATABASE_URL 则自动切换 PostgreSQL
（免费云平台如 Render 重启后文件系统会重置，必须用 Postgres 持久化）
"""
import os
import json
import time
from datetime import datetime, timedelta

import config

# 是否使用 PostgreSQL（通过 DATABASE_URL 环境变量开启）
PG = bool(os.environ.get('DATABASE_URL'))
PH = '%s' if PG else '?'   # 占位符


def get_conn():
    if PG:
        import psycopg2
        import psycopg2.extras
        conn = psycopg2.connect(os.environ['DATABASE_URL'])
        conn.autocommit = True
        return conn
    os.makedirs(config.DATA_DIR, exist_ok=True)
    restore_from_backup()
    import sqlite3
    conn = sqlite3.connect(config.DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    # 对象存储挂载不支持 WAL 的多文件读写，使用普通日志模式
    conn.execute('PRAGMA journal_mode=DELETE')
    return conn


def restore_from_backup():
    """启动时从对象存储挂载目录恢复数据库备份"""
    if PG or not config.BACKUP_DIR:
        return
    try:
        os.makedirs(config.BACKUP_DIR, exist_ok=True)
        backup_path = os.path.join(config.BACKUP_DIR, 'radar.db')
        if not os.path.exists(backup_path) or os.path.getsize(backup_path) == 0:
            return
        # 本地无数据库，或备份比本地新/大，则恢复
        if (not os.path.exists(config.DB_PATH) or
                os.path.getsize(backup_path) > os.path.getsize(config.DB_PATH)):
            import shutil
            shutil.copy2(backup_path, config.DB_PATH)
    except Exception:
        pass


_last_backup_ts = [0.0]   # 上次全库备份时间戳（进程级节流）


def backup_db(force=False):
    """把 SQLite 数据库备份到对象存储挂载目录，容器重启后可恢复。

    节流：默认 BACKUP_INTERVAL 秒内只备份一次（此前逐条写库都全量拷贝，
    存档全文/图片入库后库会到几十上百 MB，逐条备份会拖垮采集）。
    force=True 用于采集结束/设置保存等关键节点，绕过节流立即备份。
    """
    if PG or not config.BACKUP_DIR:
        return
    now = time.time()
    if not force and now - _last_backup_ts[0] < config.BACKUP_INTERVAL:
        return
    _last_backup_ts[0] = now
    try:
        os.makedirs(config.BACKUP_DIR, exist_ok=True)
        import sqlite3
        import shutil
        # 先 checkpoint，确保 WAL 合并到主库（即使当前不是 WAL 模式也安全）
        conn = sqlite3.connect(config.DB_PATH, timeout=10)
        conn.execute('PRAGMA wal_checkpoint(FULL)')
        conn.close()
        shutil.copy2(config.DB_PATH, os.path.join(config.BACKUP_DIR, 'radar.db'))
    except Exception:
        pass


def init_db():
    """初始化数据库表结构（兼容 SQLite / PostgreSQL）"""
    conn = get_conn()
    cur = conn.cursor()
    id_def = 'SERIAL PRIMARY KEY' if PG else 'INTEGER PRIMARY KEY AUTOINCREMENT'
    for sql in [
        '''CREATE TABLE IF NOT EXISTS intelligence (
            id %(id)s,
            date TEXT NOT NULL,
            vendor TEXT DEFAULT '',
            industry TEXT DEFAULT '',
            title TEXT NOT NULL,
            source TEXT DEFAULT '',
            url TEXT DEFAULT '',
            summary TEXT DEFAULT '',
            description TEXT DEFAULT '',
            image TEXT DEFAULT '',
            relevance INTEGER DEFAULT 3,
            tags TEXT DEFAULT '[]',
            collected_at TEXT DEFAULT '',
            is_favorite INTEGER DEFAULT 0
        )''' % {'id': id_def},
        '''CREATE TABLE IF NOT EXISTS dahua_features (
            id %(id)s,
            category TEXT DEFAULT '',
            feature_name TEXT NOT NULL,
            description TEXT DEFAULT '',
            imported_at TEXT DEFAULT ''
        )''' % {'id': id_def},
        '''CREATE TABLE IF NOT EXISTS config (
            key TEXT PRIMARY KEY,
            value TEXT DEFAULT ''
        )''',
        '''CREATE TABLE IF NOT EXISTS collect_log (
            id %(id)s,
            date TEXT DEFAULT '',
            action TEXT DEFAULT '',
            detail TEXT DEFAULT '',
            status TEXT DEFAULT 'ok',
            created_at TEXT DEFAULT ''
        )''' % {'id': id_def},
        '''CREATE TABLE IF NOT EXISTS visit_log (
            id %(id)s,
            ts TEXT NOT NULL,
            date TEXT NOT NULL,
            path TEXT DEFAULT '/',
            session_id TEXT DEFAULT '',
            referrer TEXT DEFAULT '',
            referrer_type TEXT DEFAULT 'direct',
            device TEXT DEFAULT 'pc',
            ip_addr TEXT DEFAULT '',
            region TEXT DEFAULT ''
        )''' % {'id': id_def},
        '''CREATE TABLE IF NOT EXISTS visit_session (
            session_id TEXT PRIMARY KEY,
            date TEXT DEFAULT '',
            start_ts TEXT DEFAULT '',
            last_ts TEXT DEFAULT '',
            entry_path TEXT DEFAULT '/',
            referrer TEXT DEFAULT '',
            referrer_type TEXT DEFAULT 'direct',
            device TEXT DEFAULT 'pc',
            duration_sec INTEGER DEFAULT 0
        )''',
        '''CREATE TABLE IF NOT EXISTS visit_daily (
            date TEXT NOT NULL,
            path TEXT NOT NULL,
            hits INTEGER DEFAULT 0,
            sessions INTEGER DEFAULT 0,
            PRIMARY KEY (date, path)
        )''',
        '''CREATE TABLE IF NOT EXISTS article_archive (
            id %(id)s,
            intel_id INTEGER NOT NULL,
            url TEXT DEFAULT '',
            base_url TEXT DEFAULT '',
            html TEXT DEFAULT '',
            plain_text TEXT DEFAULT '',
            fetched_at TEXT DEFAULT ''
        )''' % {'id': id_def},
        'CREATE INDEX IF NOT EXISTS idx_archive_intel ON article_archive(intel_id)',
        'CREATE INDEX IF NOT EXISTS idx_intel_date ON intelligence(date)',
        'CREATE INDEX IF NOT EXISTS idx_intel_vendor ON intelligence(vendor)',
        'CREATE INDEX IF NOT EXISTS idx_intel_industry ON intelligence(industry)',
        'CREATE INDEX IF NOT EXISTS idx_visitlog_date ON visit_log(date)',
        'CREATE INDEX IF NOT EXISTS idx_visitlog_path ON visit_log(path)',
        'CREATE INDEX IF NOT EXISTS idx_visitsess_date ON visit_session(date)',
    ]:
        cur.execute(sql)

    # 兼容老库：补充 image / published / media 字段
    try:
        if PG:
            cur.execute("ALTER TABLE intelligence ADD COLUMN IF NOT EXISTS image TEXT DEFAULT ''")
            cur.execute("ALTER TABLE intelligence ADD COLUMN IF NOT EXISTS published TEXT DEFAULT ''")
            cur.execute("ALTER TABLE intelligence ADD COLUMN IF NOT EXISTS media TEXT DEFAULT ''")
        else:
            cur.execute("PRAGMA table_info(intelligence)")
            cols = [r[1] for r in cur.fetchall()]
            if 'image' not in cols:
                cur.execute("ALTER TABLE intelligence ADD COLUMN image TEXT DEFAULT ''")
            if 'published' not in cols:
                cur.execute("ALTER TABLE intelligence ADD COLUMN published TEXT DEFAULT ''")
            if 'media' not in cols:
                cur.execute("ALTER TABLE intelligence ADD COLUMN media TEXT DEFAULT ''")
    except Exception as e:
        database_log = None  # 迁移失败不应阻塞启动

    # 兼容老库：补充 visit_log 的 ip_addr / region 字段
    try:
        if PG:
            cur.execute("ALTER TABLE visit_log ADD COLUMN IF NOT EXISTS ip_addr TEXT DEFAULT ''")
            cur.execute("ALTER TABLE visit_log ADD COLUMN IF NOT EXISTS region TEXT DEFAULT ''")
        else:
            cur.execute("PRAGMA table_info(visit_log)")
            vcols = [r[1] for r in cur.fetchall()]
            if 'ip_addr' not in vcols:
                cur.execute("ALTER TABLE visit_log ADD COLUMN ip_addr TEXT DEFAULT ''")
            if 'region' not in vcols:
                cur.execute("ALTER TABLE visit_log ADD COLUMN region TEXT DEFAULT ''")
    except Exception:
        pass

    conn.commit()
    conn.close()
    backup_db(force=True)


def query(sql, args=()):
    conn = get_conn()
    try:
        if PG:
            cur = conn.cursor()
            cur.execute(sql, tuple(args))
            cols = [d[0] for d in cur.description] if cur.description else []
            rows = [dict(zip(cols, r)) for r in cur.fetchall()]
            return rows
        rows = conn.execute(sql, tuple(args)).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def query_one(sql, args=()):
    rows = query(sql, args)
    return rows[0] if rows else None


def execute(sql, args=()):
    """执行写操作；INSERT 返回自增ID"""
    conn = get_conn()
    try:
        if PG:
            cur = conn.cursor()
            if sql.strip().upper().startswith('INSERT'):
                cur.execute(sql + ' RETURNING id', tuple(args))
                row = cur.fetchone()
                conn.commit()
                return row[0] if row else None
            cur.execute(sql, tuple(args))
            conn.commit()
            return None
        cur = conn.execute(sql, tuple(args))
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def get_config(key, default=''):
    row = query_one('SELECT value FROM config WHERE key=' + PH, (key,))
    return row['value'] if row else default


def set_config(key, value):
    if PG:
        execute(
            'INSERT INTO config(key,value) VALUES(%s,%s) '
            'ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value',
            (key, str(value)))
    else:
        execute(
            'INSERT INTO config(key,value) VALUES(?,?) '
            'ON CONFLICT(key) DO UPDATE SET value=excluded.value',
            (key, str(value)))


def today_str():
    return datetime.now().strftime('%Y-%m-%d')


def now_str():
    return datetime.now().strftime('%Y-%m-%d %H:%M:%S')


def add_intelligence(item):
    """新增一条情报；按标题/URL 全局去重（跨日期），已存在返回 False，新增返回自增 id"""
    title = item.get('title', '')
    url = item.get('url', '')
    exist = query_one(
        'SELECT id FROM intelligence WHERE title=' + PH + " OR (url!='' AND url=" + PH + ')',
        (title, url))
    if exist:
        return False
    placeholders = ','.join([PH] * 14)
    new_id = execute('''
        INSERT INTO intelligence
        (date, vendor, industry, title, source, url, summary, description, image, media, relevance, tags, published, collected_at)
        VALUES (''' + placeholders + ''')''', (
        item.get('date', today_str()),
        item.get('vendor', ''),
        item.get('industry', ''),
        item.get('title', ''),
        item.get('source', ''),
        item.get('url', ''),
        item.get('summary', ''),
        item.get('description', ''),
        item.get('image', ''),
        json.dumps(item.get('media') or {}, ensure_ascii=False),
        int(item.get('relevance', 3)),
        json.dumps(item.get('tags', []), ensure_ascii=False),
        item.get('published', ''),
        now_str(),
    ))
    backup_db()
    return new_id if new_id else True


def save_archive(intel_id, url='', base_url='', html='', plain_text=''):
    """保存/覆盖一条情报的全文存档（净化后 HTML + 纯文本）"""
    if not intel_id or not (html or plain_text):
        return False
    execute('DELETE FROM article_archive WHERE intel_id=' + PH, (intel_id,))
    execute('INSERT INTO article_archive(intel_id,url,base_url,html,plain_text,fetched_at) '
            'VALUES (' + ','.join([PH] * 6) + ')',
            (intel_id, url or '', base_url or '', html or '', plain_text or '', now_str()))
    backup_db()
    return True


def get_archive(intel_id):
    """读取一条情报的全文存档"""
    return query_one('SELECT * FROM article_archive WHERE intel_id=' + PH, (intel_id,))


def delete_intelligence(iid):
    """删除一条情报及其关联存档（防止 article_archive 留孤儿数据）"""
    execute('DELETE FROM intelligence WHERE id=' + PH, (iid,))
    execute('DELETE FROM article_archive WHERE intel_id=' + PH, (iid,))


def purge_intelligence(keep_days=None):
    """按保留期清理过期情报与关联存档；返回 (删除情报数, 删除存档数)。

    keep_days 缺省读 config 表 intel_retention_days（后台可改），再退回
    config.INTEL_RETENTION_DAYS；<=0 表示永不过期。
    """
    if keep_days is None:
        try:
            keep_days = int(get_config('intel_retention_days', '') or config.INTEL_RETENTION_DAYS)
        except (TypeError, ValueError):
            keep_days = config.INTEL_RETENTION_DAYS
    try:
        keep_days = int(keep_days)
    except (TypeError, ValueError):
        keep_days = config.INTEL_RETENTION_DAYS
    if keep_days <= 0:
        return 0, 0
    cut = (datetime.now() - timedelta(days=keep_days)).strftime('%Y-%m-%d')
    c1 = query_one('SELECT COUNT(*) AS c FROM intelligence WHERE date<' + PH, (cut,))['c']
    if c1:
        execute('DELETE FROM intelligence WHERE date<' + PH, (cut,))
    c2 = query_one('SELECT COUNT(*) AS c FROM article_archive WHERE intel_id NOT IN '
                   '(SELECT id FROM intelligence)')['c']
    if c2:
        execute('DELETE FROM article_archive WHERE intel_id NOT IN (SELECT id FROM intelligence)')
    return c1, c2


def log(action, detail='', status='ok'):
    execute('INSERT INTO collect_log(date, action, detail, status, created_at) VALUES ('
            + PH + ',' + PH + ',' + PH + ',' + PH + ',' + PH + ')',
            (today_str(), action, detail, status, now_str()))
    backup_db()


def stats():
    """今日条数 / 收藏数 / 总计 / 厂商数"""
    today = today_str()
    return {
        'today': query_one('SELECT COUNT(*) AS c FROM intelligence WHERE date=' + PH, (today,))['c'],
        'favorites': query_one('SELECT COUNT(*) AS c FROM intelligence WHERE is_favorite=1')['c'],
        'total': query_one('SELECT COUNT(*) AS c FROM intelligence')['c'],
        'vendors': query_one("SELECT COUNT(DISTINCT vendor) AS c FROM intelligence WHERE vendor!=''")['c'],
    }


# ==================== 访问统计 ====================
def record_visit_batch(records):
    """批量落库访问统计：一个连接一个事务写完全部记录，降低高并发下的连接压力
    records: [{'kind':'hit','ts','path','sid','referrer','rtype','device'},
              {'kind':'duration','ts','sid','duration'}]
    """
    if not records:
        return
    hits = [r for r in records if r.get('kind') == 'hit']
    durs = [r for r in records if r.get('kind') == 'duration']
    conn = get_conn()
    try:
        cur = conn.cursor()
        # 1) 原始访问 + 日聚合
        for r in hits:
            ts = str(r.get('ts', ''))[:19]
            d = ts[:10] or today_str()
            path = str(r.get('path', '/') or '/')[:190]
            sid = str(r.get('sid', '') or '')[:64]
            cur.execute('INSERT INTO visit_log(ts,date,path,session_id,referrer,referrer_type,device,ip_addr,region) '
                        'VALUES (' + ','.join([PH] * 9) + ')',
                        (ts, d, path, sid,
                         str(r.get('referrer', ''))[:380],
                         str(r.get('rtype', 'direct'))[:16],
                         str(r.get('device', 'pc'))[:8],
                         str(r.get('ip', ''))[:45],
                         str(r.get('region', ''))[:64]))
            cur.execute('INSERT INTO visit_daily(date,path,hits,sessions) VALUES (' + PH + ',' + PH + ',1,0) '
                        'ON CONFLICT(date,path) DO UPDATE SET hits=visit_daily.hits+1', (d, path))
        # 2) 会话：先查出已存在的，新的插入（并计入 sessions 聚合），旧的仅更新 last_ts
        sids = {}
        for r in hits:
            sid = str(r.get('sid', '') or '')[:64]
            if sid and sid not in sids:   # 首见优先：会话入口取首次访问
                sids[sid] = r
        if sids:
            ph_in = ','.join([PH] * len(sids))
            cur.execute('SELECT session_id FROM visit_session WHERE session_id IN (' + ph_in + ')',
                        tuple(sids.keys()))
            existing = {row[0] for row in cur.fetchall()}
            for sid, r in sids.items():
                ts = str(r.get('ts', ''))[:19]
                d = ts[:10] or today_str()
                path = str(r.get('path', '/') or '/')[:190]
                if sid in existing:
                    cur.execute('UPDATE visit_session SET last_ts=' + PH + ' WHERE session_id=' + PH,
                                (ts, sid))
                else:
                    cur.execute('''INSERT INTO visit_session
                        (session_id,date,start_ts,last_ts,entry_path,referrer,referrer_type,device,duration_sec)
                        VALUES (''' + ','.join([PH] * 9) + ')',
                        (sid, d, ts, ts, path,
                         str(r.get('referrer', ''))[:380],
                         str(r.get('rtype', 'direct'))[:16],
                         str(r.get('device', 'pc'))[:8], 0))
                    cur.execute('INSERT INTO visit_daily(date,path,hits,sessions) VALUES (' + PH + ',' + PH + ',0,1) '
                                'ON CONFLICT(date,path) DO UPDATE SET sessions=visit_daily.sessions+1', (d, path))
        # 3) 停留时长：只增不减（心跳/离场上报的秒数取最大值）
        for r in durs:
            sid = str(r.get('sid', '') or '')[:64]
            if not sid:
                continue
            try:
                dur = int(float(r.get('duration', 0)))
            except (TypeError, ValueError):
                dur = 0
            if dur <= 0:
                continue
            dur = min(dur, 86400)
            ts = str(r.get('ts', ''))[:19]
            cur.execute('UPDATE visit_session SET duration_sec=CASE WHEN duration_sec<' + PH + ' THEN ' + PH +
                        ' ELSE duration_sec END, last_ts=' + PH + ' WHERE session_id=' + PH,
                        (dur, dur, ts, sid))
        conn.commit()
    finally:
        conn.close()


def analytics_query(days):
    """访问统计报表原始聚合（报表只读 visit_daily 聚合表，不扫原始大表）"""
    from datetime import timedelta
    start = (datetime.now() - timedelta(days=days - 1)).strftime('%Y-%m-%d')
    data = {
        'daily': query('SELECT date, SUM(hits) AS hits, SUM(sessions) AS sessions '
                       'FROM visit_daily WHERE date>=' + PH + ' GROUP BY date ORDER BY date', (start,)),
        'pages': query('SELECT path, SUM(hits) AS hits, SUM(sessions) AS sessions '
                       'FROM visit_daily WHERE date>=' + PH + ' GROUP BY path '
                       'ORDER BY hits DESC LIMIT 15', (start,)),
        'sources': query('SELECT referrer_type AS t, COUNT(*) AS c FROM visit_log '
                         'WHERE date>=' + PH + ' GROUP BY referrer_type', (start,)),
        'referrers': query("SELECT referrer, COUNT(*) AS c FROM visit_log WHERE date>=" + PH +
                           " AND referrer_type IN ('search','external') AND referrer!='' "
                           'GROUP BY referrer ORDER BY c DESC LIMIT 10', (start,)),
        'sess': query_one('SELECT COUNT(*) AS c, COALESCE(AVG(duration_sec),0) AS avg_d, '
                          'COALESCE(MAX(duration_sec),0) AS max_d FROM visit_session WHERE date>=' + PH,
                          (start,)) or {'c': 0, 'avg_d': 0, 'max_d': 0},
        'recent': query('SELECT ts, path, referrer_type, device, session_id, ip_addr, region FROM visit_log '
                        'WHERE date>=' + PH + ' ORDER BY id DESC LIMIT 20', (start,)),
    }
    return data


def visit_detail_query(page=1, page_size=50, days=30):
    """访问明细分页查询（按时间倒序，含 IP / 地区）"""
    from datetime import timedelta
    start = (datetime.now() - timedelta(days=days - 1)).strftime('%Y-%m-%d')
    offset = (page - 1) * page_size
    total = query_one('SELECT COUNT(*) AS c FROM visit_log WHERE date>=' + PH, (start,)) or {'c': 0}
    items = query('SELECT ts, date, path, session_id, referrer, referrer_type, device, ip_addr, region '
                  'FROM visit_log WHERE date>=' + PH + ' ORDER BY id DESC LIMIT ' + str(int(page_size)) +
                  ' OFFSET ' + str(int(offset)), (start,))
    return {'total': total['c'] or 0, 'items': items, 'page': page, 'page_size': page_size, 'days': days}


def purge_visit_log(keep_days=90):
    """清理超龄访问原始数据（聚合表 visit_daily 保留，体积小）"""
    from datetime import timedelta
    cut = (datetime.now() - timedelta(days=keep_days)).strftime('%Y-%m-%d')
    execute('DELETE FROM visit_log WHERE date<' + PH, (cut,))
    execute('DELETE FROM visit_session WHERE date<' + PH, (cut,))
