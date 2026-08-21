# -*- coding: utf-8 -*-
"""行业情报雷达 - 数据层（兼容 SQLite 本地 / PostgreSQL 云端）
本地默认 SQLite；若设置了环境变量 DATABASE_URL 则自动切换 PostgreSQL
（免费云平台如 Render 重启后文件系统会重置，必须用 Postgres 持久化）
"""
import os
import json
from datetime import datetime

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
    import sqlite3
    conn = sqlite3.connect(config.DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA journal_mode=WAL')
    return conn


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
        'CREATE INDEX IF NOT EXISTS idx_intel_date ON intelligence(date)',
        'CREATE INDEX IF NOT EXISTS idx_intel_vendor ON intelligence(vendor)',
        'CREATE INDEX IF NOT EXISTS idx_intel_industry ON intelligence(industry)',
    ]:
        cur.execute(sql)
    conn.commit()
    conn.close()


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
    """新增一条情报；若同标题+日期已存在则跳过"""
    exist = query_one(
        'SELECT id FROM intelligence WHERE date=' + PH + ' AND title=' + PH,
        (item.get('date', today_str()), item.get('title', '')))
    if exist:
        return False
    placeholders = ','.join([PH] * 11)
    execute('''
        INSERT INTO intelligence
        (date, vendor, industry, title, source, url, summary, description, relevance, tags, collected_at)
        VALUES (''' + placeholders + ''')''', (
        item.get('date', today_str()),
        item.get('vendor', ''),
        item.get('industry', ''),
        item.get('title', ''),
        item.get('source', ''),
        item.get('url', ''),
        item.get('summary', ''),
        item.get('description', ''),
        int(item.get('relevance', 3)),
        json.dumps(item.get('tags', []), ensure_ascii=False),
        now_str(),
    ))
    return True


def log(action, detail='', status='ok'):
    execute('INSERT INTO collect_log(date, action, detail, status, created_at) VALUES ('
            + PH + ',' + PH + ',' + PH + ',' + PH + ',' + PH + ')',
            (today_str(), action, detail, status, now_str()))


def stats():
    """今日条数 / 收藏数 / 总计 / 厂商数"""
    today = today_str()
    return {
        'today': query_one('SELECT COUNT(*) AS c FROM intelligence WHERE date=' + PH, (today,))['c'],
        'favorites': query_one('SELECT COUNT(*) AS c FROM intelligence WHERE is_favorite=1')['c'],
        'total': query_one('SELECT COUNT(*) AS c FROM intelligence')['c'],
        'vendors': query_one("SELECT COUNT(DISTINCT vendor) AS c FROM intelligence WHERE vendor!=''")['c'],
    }
