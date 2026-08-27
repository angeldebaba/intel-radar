# -*- coding: utf-8 -*-
"""每日关注 - 五大维度情报聚合模块

将已入库的 intelligence 情报按「行业动态 / 产品 / 技术 / 市场 / 关注点」
五个维度归并展示，为公开页「每日关注」栏目提供结构化数据。

设计要点：
- 纯读操作，复用现有采集/评分/tags 体系，不改动采集主链路。
- 分类采用「标签优先 + 关键词打分 + 兜底规则」三层判定，每条可叠加多维度。
- 快照可缓存（存 config 表，key = daily_focus:YYYY-MM-DD），
  每日采集完成后自动生成，也可后台手动刷新。
"""
import json
import time
from datetime import date

import config
import database

# ==================== 维度定义 ====================
DIMENSIONS = ['industry', 'product', 'technology', 'market', 'watch']
DIM_NAMES = {
    'industry': '行业动态',
    'product': '产品',
    'technology': '技术',
    'market': '市场',
    'watch': '关注点',
}
DIM_ICONS = {
    'industry': '🌐',
    'product': '📦',
    'technology': '🔬',
    'market': '📈',
    'watch': '⭐',
}
DIM_DESCS = {
    'industry': '政策法规、行业案例、展会活动等宏观动态',
    'product': '厂商新发布的引擎、平台、系统与版本更新',
    'technology': '三维/引擎/BIM/GIS等底层技术与架构演进',
    'market': '融资、合作、中标、市场份额等商业信号',
    'watch': '高相关度、多方讨论的重点信息，建议重点关注',
}

# ==================== 关键词词表（可迭代） ====================
# 产品词（避免过于宽泛的词如"产品/软件"造成误判，聚焦具体产品信号）
PRODUCT_WORDS = ['引擎', '平台', '系统', '版本', '发布', '上线', '推出', '新一代',
                 '工具', '套件', '桌面端', '客户端', '控件', 'SDK', 'API',
                 '解决方案', '产品化', '发布', '迭代更新']
# 技术词
TECH_WORDS = ['三维', '3D', '引擎', 'BIM', 'GIS', 'CIM', '算法', '架构', '渲染',
              '建模', '数据底座', '孪生底座', '时空底座', '点云', '倾斜摄影',
              '激光', 'AI', '大模型', '深度学习', '可视化引擎']
# 市场词
MARKET_WORDS = ['融资', '投资', '收购', '合作', '战略', '中标', '签约', '采购',
                '订单', '份额', '市场', '亿元', '万', '收入', '营收', '财报',
                '客户', '落地', '部署', '交付', '商业化', '招标']
# 关注点词（高相关 + 多信号叠加，见规则）
WATCH_WORDS = ['首', '全球', '突破', '颠覆', '引领', '第一', '里程碑',
               '独家', '重磅', '核心', '关键', '落地案例', '标杆']


def _hit(text, words):
    """判断 text 是否命中任一关键词"""
    if not text:
        return False
    low = text.lower()
    for w in words:
        if w.lower() in low:
            return True
    return False


def _has_tag(tags, name):
    return name in (tags or [])


def classify_item(item):
    """对单条情报做五维归类，返回维度列表（可叠加）"""
    dims = set()
    title = item.get('title') or ''
    desc = (item.get('description') or '') + ' ' + (item.get('summary') or '')
    text = title + ' ' + desc
    tags = item.get('tags') or []
    relevance = item.get('relevance') or 0

    # 1) 行业动态：标签命中 政策/案例/展会，或含行业属性词
    if _has_tag(tags, '政策') or _has_tag(tags, '案例') or _has_tag(tags, '展会') \
            or _has_tag(tags, '动态'):
        dims.add('industry')

    # 2) 产品：标签「方案」或命中产品词
    if _has_tag(tags, '方案') or _hit(text, PRODUCT_WORDS):
        dims.add('product')

    # 3) 技术：标签「技术」或命中技术词
    if _has_tag(tags, '技术') or _hit(text, TECH_WORDS):
        dims.add('technology')

    # 4) 市场：命中市场词
    if _hit(text, MARKET_WORDS):
        dims.add('market')

    # 5) 关注点：高相关度(4/5) 或 命中重点词 且 相关度不低(>=3)
    if relevance >= 4 or (_hit(text, WATCH_WORDS) and relevance >= 3):
        dims.add('watch')

    # 兜底：以上都未命中，至少归入「行业动态」，避免条目被丢弃
    if not dims:
        dims.add('industry')

    return sorted(dims)


# ==================== 聚合主逻辑 ====================
def build_focus(date_str=None, days=None):
    """按指定日期窗口聚合五维情报。

    - date_str: 快照日期（YYYY-MM-DD），缺省为今天
    - days: 窗口天数，缺省取 config.DAILY_FOCUS_DAYS
    返回 {dim: {items:[...], total, desc...}}，每维度按相关度降序。
    """
    if days is None:
        days = config.DAILY_FOCUS_DAYS
    if not date_str:
        date_str = date.today().isoformat()

    # 计算窗口起始日期
    try:
        from datetime import datetime, timedelta
        d = datetime.strptime(date_str, '%Y-%m-%d').date()
        start = (d - timedelta(days=max(1, days) - 1)).isoformat()
    except Exception:
        start = date_str

    rows = database.query(
        'SELECT * FROM intelligence WHERE date>=? ORDER BY relevance DESC, id DESC',
        (start,))
    per_dim = config.DAILY_FOCUS_PER_DIM

    result = {}
    for dim in DIMENSIONS:
        result[dim] = {
            'name': DIM_NAMES[dim],
            'icon': DIM_ICONS[dim],
            'desc': DIM_DESCS[dim],
            'items': [],
            'total': 0,
        }

    for r in rows:
        try:
            r['tags'] = json.loads(r.get('tags') or '[]')
        except (ValueError, TypeError):
            r['tags'] = []
        # 与 /api/intelligence 保持一致地解析 media
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

        for dim in classify_item(r):
            bucket = result[dim]
            if len(bucket['items']) < per_dim:
                bucket['items'].append({
                    'id': r.get('id'),
                    'title': r.get('title'),
                    'source': r.get('source'),
                    'vendor': r.get('vendor'),
                    'industry': r.get('industry'),
                    'url': r.get('url'),
                    'summary': r.get('summary'),
                    'description': r.get('description'),
                    'image': r.get('image'),
                    'relevance': r.get('relevance'),
                    'tags': r.get('tags'),
                    'date': r.get('date'),
                    'media': r.get('media'),
                })
            bucket['total'] += 1

    return {
        'date': date_str,
        'days': days,
        'window_start': start,
        'dimensions': result,
        'generated_at': database.now_str(),
    }


# ==================== 快照缓存 ====================
def cache_key(date_str):
    return 'daily_focus:{}'.format(date_str)


def save_focus_snapshot(date_str=None, days=None):
    """生成并缓存某日快照（存 config 表）。返回快照 dict。"""
    data = build_focus(date_str, days)
    database.set_config(cache_key(data['date']), json.dumps(data, ensure_ascii=False))
    return data


def load_focus_snapshot(date_str=None):
    """读取已缓存的快照；不存在则实时计算（不写缓存）。"""
    if not date_str:
        date_str = date.today().isoformat()
    raw = database.get_config(cache_key(date_str), '')
    if raw:
        try:
            return json.loads(raw)
        except (ValueError, TypeError):
            pass
    return build_focus(date_str)


def generate_today():
    """每晚采集后调用：生成当日快照。返回新增/更新时间戳，用于日志。"""
    data = save_focus_snapshot()
    return data['date'], data['generated_at']
