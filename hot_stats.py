# -*- coding: utf-8 -*-
"""数字孪生行业热点统计模块

每晚采集完成后聚合 intelligence 表，生成一份"热点看板"快照存 config 表：
- 总览指标（总量、近7/30天新增、来源/厂商/行业/标签/相关度分布）
- 近 30 天每日新增趋势
- 厂商 / 行业 / 来源 Top N
- 热词 Top N（基于标题+AI摘要，停用词过滤 + 高频短语抽取）
- 高相关度头条 Top N

设计原则：
- 纯读 + 一次写，不改动采集主链路
- 快照存 config 表，key = hot_stats:YYYY-MM-DD；公开接口读快照，不实时全表扫
- 后台可手动刷新
"""
import json
import os
import re
from collections import Counter
from datetime import date, datetime, timedelta

import config
import database

# 快照窗口（可通过环境变量 HOT_STATS_TREND_DAYS 覆盖，默认 30 天）
TREND_DAYS = int(os.environ.get('HOT_STATS_TREND_DAYS', '30') or '30')
TOP_VENDORS = 15
TOP_INDUSTRIES = 15
TOP_SOURCES = 12
TOP_TAGS = 20
TOP_KEYWORDS = 25
TOP_ARTICLES = 10

# ==================== 热词抽取 ====================
# 领域词典：数字孪生/视频融合行业内常被当作"热点信号"的术语。
# 命中即按整体短语计数，避免被切成单字。
DOMAIN_TERMS = [
    '数字孪生', '视频融合', '视频孪生', '三维可视化', '三维重建', '三维引擎',
    '三维GIS', '三维建模', '三维场景', '数字底座', '孪生底座', '时空底座',
    '数据底座', '城市信息模型', '智慧城市', '智慧园区', '智慧工厂', '智慧医院',
    '智慧校园', '智慧景区', '智慧建筑', '智慧交通', '智慧能源', '智慧水利',
    '智慧应急', '智慧安防', '智慧物流', '智慧矿山', '智慧电力', '智慧政务',
    '实景三维', '数字高程', '倾斜摄影', '点云', '激光点云', 'BIM', 'GIS',
    'CIM', 'IoT', '数字人', '元宇宙', 'AIGC', '大模型', '人工智能',
    '工业互联网', '工业4.0', '工业元宇宙', '虚拟现实', '增强现实', '混合现实',
    '虚拟现实VR', 'AR', 'VR', 'XR', '数字孪生流域', '数字孪生水利',
    '数字孪生城市', '数字孪生工厂', '数字孪生电网', '数字孪生交通', '数字孪生校园',
    '数字孪生医院', '数字孪生景区', '数字孪生建筑', '数字孪生产线',
    '低空经济', '无人机', '无人车', '机器人', '边缘计算', '云计算',
    '物联网', '5G', '北斗', '时空大数据', '地理信息', '卫星遥感',
    '视频监控', '视频结构化', '视频图像', '视频中台', '视频云',
    '可视化平台', '可视化大屏', '大屏可视化', '决策大屏', '驾驶舱',
    '海康威视', '大华股份', '华为', '腾讯', '阿里', '百度', '商汤',
    '51WORLD', '优锘科技', '超图软件', '数字冰雹', '智汇云舟',
    '易知微', '飞渡科技', '趣境科技', '飞影光影', '泰瑞数创',
    '英伟达', 'NVIDIA', 'Omniverse', 'Unity', 'Unreal', '虚幻引擎',
    '发布会', '战略合作', '签约', '中标', '融资', '上市', '生态合作',
]

# 停用词：高频但无信息量的词，统计前剔除
STOP_WORDS = set('''
的 了 在 是 我 有 和 就 不 人 都 一 一个 上 也 很 到 说 要 去 你 会 着 没有 看 好 自己 这
那 他 她 它 们 我们 你们 他们 这个 那个 这些 那些 什么 怎么 为什么 如何 可以 已经 但是 因为
所以 如果 还是 或者 以及 等等 之一 相关 关于 通过 进行 实现 提供 支持 打造 构建 建设 发展
推动 开展 完成 加强 提升 促进 公司 集团 有限 有限公司 科技 技术 项目 方案 解决 方案解决
平台 系统 产品 业务 行业 企业 用户 客户 市场 数据 服务 应用 场景 能力 功能 版本 发布 升级
新 最新 今日 近日 日前 当天 当时 今年 去年 明年 目前 未来 近期 期间 之后 之前 同时 其中
更多 详细 全文 阅读 原文 点击 这里 查看 了解 介绍 报道 显示 显示 新闻 资讯 消息 公告
'''.split())

# 中文词抽取：2~6 字连续汉字（剔除纯停用词、过短/过长）
_CN_RUN = re.compile(r'[\u4e00-\u9fa5A-Za-z0-9\+\#\.\-]{2,8}')


def _iter_terms(text):
    """从一段文本中产出候选词：先整段匹配领域词典，再切 2-8 字中英混排串。"""
    if not text:
        return
    # 领域术语按命中统计（多词可能重叠，用 finditer 按出现顺序）
    for term in DOMAIN_TERMS:
        if term in text:
            # 同一篇里出现多次只算一次（避免某条目重复堆砌刷词频）
            yield term
    # 再切常规中英混排 token，做候选
    for m in _CN_RUN.findall(text):
        t = m.strip()
        if len(t) < 2 or t in STOP_WORDS:
            continue
        if t.isdigit() or re.match(r'^[\d\.\-]+$', t):
            continue
        # 全英文/数字混合的专有名词才保留（BIM/GIS/5G/AIoT 等）
        if re.match(r'^[A-Za-z0-9\+\#\.\-]+$', t):
            if len(t) <= 8:
                yield t
            continue
        # 中文 2~6 字且不是全停用字
        cn = re.sub(r'[A-Za-z0-9\+\#\.\-]', '', t)
        if 2 <= len(cn) <= 6 and not all(ch in STOP_WORDS for ch in cn):
            yield t


def _extract_keywords(rows, top_n=TOP_KEYWORDS):
    """从情报标题+摘要中抽取热词。每条情报对同一词最多贡献 1 次，避免被长文刷屏。"""
    counter = Counter()
    for r in rows:
        text = ((r.get('title') or '') + ' ' + (r.get('summary') or '')
                + ' ' + (r.get('description') or ''))
        seen = set()
        for term in _iter_terms(text):
            if term in seen:
                continue
            seen.add(term)
            counter[term] += 1
    # 领域术语加权：天然有信号，+1 权重让它在长尾中更易出头
    for term in list(counter.keys()):
        if term in DOMAIN_TERMS:
            counter[term] += 1
    return [{'word': w, 'count': c} for w, c in counter.most_common(top_n)]


# ==================== 主聚合 ====================
def build_stats(days=TREND_DAYS):
    """聚合热点看板数据。

    返回 dict：
    {
      date, generated_at, window_days, window_start,
      overview: {total, today, last_7d, last_30d, high_rel, sources, vendors, industries},
      trend_daily: [{date, count, high_rel}],
      vendors: [{name, count}],
      industries: [{name, count}],
      tags: [{tag, count}],
      sources: [{source, count}],
      relevance: [{score, count}],
      keywords: [{word, count}],
      top_articles: [{id, title, vendor, industry, relevance, url, date, summary}]
    }
    """
    today = date.today()
    window_start = (today - timedelta(days=days - 1)).isoformat()
    week_start = (today - timedelta(days=6)).isoformat()
    today_s = today.isoformat()

    ph = database.PH

    # 总量（不限制窗口）
    total = database.query_one('SELECT COUNT(*) AS c FROM intelligence')['c']

    # 窗口内全量行（用于热词、来源/厂商/行业分布、Top）
    rows = database.query(
        'SELECT id,title,summary,description,vendor,industry,source,url,'
        'relevance,tags,date,published,collected_at,is_favorite '
        'FROM intelligence WHERE date>=' + ph + ' ORDER BY relevance DESC, id DESC',
        (window_start,))

    today_count = sum(1 for r in rows if r['date'] == today_s)
    week_count = sum(1 for r in rows if r['date'] >= week_start)
    high_rel_count = sum(1 for r in rows if (r.get('relevance') or 0) >= 4)

    # 每日趋势（近 N 天）
    daily_map = {}
    high_daily_map = {}
    for r in rows:
        d = r['date']
        daily_map[d] = daily_map.get(d, 0) + 1
        if (r.get('relevance') or 0) >= 4:
            high_daily_map[d] = high_daily_map.get(d, 0) + 1
    trend_daily = []
    for i in range(days - 1, -1, -1):
        d = (today - timedelta(days=i)).isoformat()
        trend_daily.append({
            'date': d,
            'count': daily_map.get(d, 0),
            'high_rel': high_daily_map.get(d, 0),
        })

    # 厂商 / 行业 / 来源 / 标签 / 相关度
    vendor_c, industry_c, source_c, tag_c, rel_c = Counter(), Counter(), Counter(), Counter(), Counter()
    for r in rows:
        if r.get('vendor'):
            vendor_c[r['vendor']] += 1
        if r.get('industry'):
            industry_c[r['industry']] += 1
        src = (r.get('source') or '').strip() or '网络'
        source_c[src] += 1
        try:
            for t in json.loads(r.get('tags') or '[]'):
                tag_c[t] += 1
        except (ValueError, TypeError):
            pass
        rel_c[int(r.get('relevance') or 0)] += 1

    # 标签云（包含全量数据，不只窗口——热门标签是长期信号）
    if not tag_c:
        for r in database.query('SELECT tags FROM intelligence'):
            try:
                for t in json.loads(r.get('tags') or '[]'):
                    tag_c[t] += 1
            except (ValueError, TypeError):
                pass

    # 相关度分布按 1-5 分桶
    relevance = [{'score': s, 'count': rel_c.get(s, 0)} for s in (5, 4, 3, 2, 1)]

    # 热词
    keywords = _extract_keywords(rows, top_n=TOP_KEYWORDS)

    # 高相关度头条（窗口内）
    top_articles = []
    for r in rows[:TOP_ARTICLES * 3]:
        if (r.get('relevance') or 0) < 4:
            continue
        top_articles.append({
            'id': r['id'],
            'title': r.get('title'),
            'vendor': r.get('vendor') or '',
            'industry': r.get('industry') or '',
            'relevance': r.get('relevance') or 0,
            'url': r.get('url') or '',
            'date': r.get('date') or '',
            'summary': (r.get('summary') or '')[:140],
        })
        if len(top_articles) >= TOP_ARTICLES:
            break

    return {
        'date': today_s,
        'generated_at': database.now_str(),
        'window_days': days,
        'window_start': window_start,
        'overview': {
            'total': total,
            'today': today_count,
            'last_7d': week_count,
            'last_30d': len(rows),
            'high_rel': high_rel_count,
            'vendors': len(vendor_c),
            'industries': len(industry_c),
            'sources': len(source_c),
        },
        'trend_daily': trend_daily,
        'vendors': [{'name': n, 'count': c} for n, c in vendor_c.most_common(TOP_VENDORS)],
        'industries': [{'name': n, 'count': c} for n, c in industry_c.most_common(TOP_INDUSTRIES)],
        'sources': [{'name': n, 'count': c} for n, c in source_c.most_common(TOP_SOURCES)],
        'tags': [{'tag': t, 'count': c} for t, c in tag_c.most_common(TOP_TAGS)],
        'relevance': relevance,
        'keywords': keywords,
        'top_articles': top_articles,
    }


# ==================== 快照缓存 ====================
def cache_key(date_str=None):
    if not date_str:
        date_str = date.today().isoformat()
    return 'hot_stats:{}'.format(date_str)


def save_snapshot(date_str=None, days=TREND_DAYS):
    """生成并缓存当日热点快照，返回快照 dict。"""
    data = build_stats(days=days)
    database.set_config(cache_key(data['date']), json.dumps(data, ensure_ascii=False))
    return data


def load_snapshot(date_str=None):
    """读取快照；不存在时实时计算（不写缓存），保证接口始终有数据返回。"""
    if not date_str:
        date_str = date.today().isoformat()
    raw = database.get_config(cache_key(date_str), '')
    if raw:
        try:
            return json.loads(raw)
        except (ValueError, TypeError):
            pass
    return build_stats()


def list_snapshot_dates(limit=30):
    """列出已有快照的日期（用于前端日期选择）。"""
    rows = database.query(
        "SELECT key FROM config WHERE key LIKE " + database.PH + " ORDER BY key DESC LIMIT " + str(int(limit)),
        ('hot_stats:%',))
    prefix = 'hot_stats:'
    return [r['key'][len(prefix):] for r in rows if r['key'].startswith(prefix)]


def generate_today():
    """每晚采集后调用：生成当日快照。"""
    data = save_snapshot()
    return data['date'], data['generated_at']
