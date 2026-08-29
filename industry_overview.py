# -*- coding: utf-8 -*-
"""
数字孪生行业全局看板（行业研究视角）。

数据不是从已抓取情报中聚合，而是一份「行业观察」结构化快照，
字段组织参考：
- 中国信通院《数字孪生发展报告》
- IDC / Gartner / MarketsandMarkets / Grand View Research
- 中国电子技术标准化研究院《数字孪生应用白皮书》
- 公开券商研报与上市公司年报

更新方式：
1) 默认返回打包在仓库内的 JSON 快照（DEFAULT_OVERVIEW）；
2) 若数据库 config 表存在 key=industry_overview:YYYY-MM-DD，优先返回该快照；
3) 后台可通过 POST /api/admin/industry-overview 写入新版快照；
4) 每晚采集任务结束后 generate_today() 都会生成「当天日期」的快照：
   以最近一份快照（或内置默认）为底稿保留静态研究内容，
   并调用 build_frontier_briefing() —— AI 立足【整个行业前沿】（不限于本站采集）
   产出顶部观察信号(watch_signals) + 六维前沿研判(frontier)；
   本站近 7 天采集情报仅作为"近期线索"锚点传入，可为空；
   AI 未配置/调用失败时保留上一版动态内容，静态内容不受影响。

文件内数值尽量标注来源与年份；不同口径的数字同时呈现，
不做盲目拼合，避免误导。
"""

from __future__ import annotations

import json
import os
import time
from typing import Any, Dict, List, Optional

import database
import config

# ---------- 默认快照 ----------
# 这是兜底数据；线上可通过后台写入新的快照覆盖
DEFAULT_OVERVIEW: Dict[str, Any] = {
    "version": "2025.08",
    "report_label": "2025-2026 行业观察",
    "generated_at": None,
    "sources": [
        {"name": "中国信通院 · 数字孪生发展报告", "year": 2024},
        {"name": "IDC · Worldwide Digital Twin Spending Guide", "year": 2024},
        {"name": "Gartner · Top Strategic Technology Trends", "year": 2025},
        {"name": "MarketsandMarkets · Digital Twin Market", "year": 2024},
        {"name": "Grand View Research · Digital Twin Market Size Report", "year": 2024},
        {"name": "中国电子技术标准化研究院 · 数字孪生应用白皮书", "year": 2023},
    ],

    # ---------- 1. 全球与中国市场规模 ----------
    "market": {
        "global_2024": {
            "value": 360,
            "unit": "亿美元",
            "yoy": 27.0,
            "source": "MarketsandMarkets 2024",
            "note": "不同口径差异较大，低口径约 180 亿、高口径约 490 亿",
        },
        "global_2030_forecast": {
            "value": 4900,
            "unit": "亿美元",
            "cagr": 53.0,
            "source": "Grand View Research 2024",
            "note": "2025-2030 CAGR 38-58%，中位数约 45%",
        },
        "china_2024": {
            "value": 220,
            "unit": "亿元",
            "yoy": 22.0,
            "source": "信通院 2024",
        },
        "china_3d_projects": {
            "value": 2000,
            "unit": "个+",
            "yoy": 45.0,
            "source": "公开招标项目统计",
            "note": "三维可视化类数字孪生项目",
        },
        "manufacturing_2025": {
            "value": 175,
            "unit": "亿元",
            "yoy": 24.0,
            "source": "信通院 / IDC 综合",
            "note": "智能制造方向",
        },
        "software_platform_2025": {
            "value": "800-900",
            "unit": "亿元",
            "yoy": 35.0,
            "source": "IDC 综合测算",
            "note": "含工业软件、仿真平台、可视化中台",
        },
        "global_forecast_bars": [
            {"year": 2024, "value": 360, "source": "M&M"},
            {"year": 2025, "value": 480, "source": "GVR"},
            {"year": 2026, "value": 690, "source": "GVR"},
            {"year": 2027, "value": 1180, "source": "GVR"},
            {"year": 2030, "value": 4900, "source": "GVR"},
        ],
    },

    # ---------- 2. 技术路线三次迁移 ----------
    "tech_shifts": [
        {
            "era": "1.0 时代 · 2000-2015",
            "title": "可视化",
            "desc": "3D 模型 + 数据大屏，偏向图形渲染、沙盘展示、决策辅助。",
            "color": "#58a6ff",
        },
        {
            "era": "2.0 时代 · 2015-2025",
            "title": "实时映射",
            "desc": "IoT + 5G 实时数据流反馈，建筑/工厂/城市能回答\"发生了什么\"。",
            "color": "#3fb950",
        },
        {
            "era": "3.0 时代 · 2025-2035",
            "title": "智能决策",
            "desc": "生成式 AI + 多智能体决策，从\"状态映射\"升级为\"预测、规划、控制\"。",
            "color": "#bc8cff",
        },
    ],

    # ---------- 3. 2026 三大核心技术突破 ----------
    "tech_breakthroughs": [
        {
            "icon": "🧬",
            "title": "视觉孪生",
            "tag": "NVIDIA OVX / 高斯泼溅",
            "desc": "基于 3DGS（3D 高斯散射）实现秒级真实场景重建，结合几何模型仿真。运维响应效率提升 40% 以上。",
            "tags": ["3DGS", "神经渲染", "实时重建"],
        },
        {
            "icon": "🌌",
            "title": "空间智能大模型",
            "tag": "CityGPT / TwinGPT",
            "desc": "在三维城市/建筑模型上接入多模态 LLM，实现自然语言驱动的场景查询、仿真推演、运营决策。",
            "tags": ["CityGPT", "多模态", "空间智能"],
        },
        {
            "icon": "📡",
            "title": "5G-A + TSN 底座",
            "tag": "Advanced 5G / Time-Sensitive Networking",
            "desc": "5G-A 部署与 TSN 深度协同，端到端时延 ≤5ms，支持百万级设备接入。",
            "tags": ["5G-A", "TSN", "确定性网络"],
        },
        {
            "icon": "🤖",
            "title": "AI 原生全栈融合",
            "tag": "Agent + Digital Twin",
            "desc": "AI Agent 成为标配，生成式 AI 自动生成孪生体、仿真脚本和运营方案，Agent 生产力提升 100x+。",
            "tags": ["AI Agent", "GenAI", "全栈融合"],
        },
    ],

    # ---------- 4. 核心应用场景分布 ----------
    "applications": {
        "china_2024_share": [
            {"name": "城市治理", "value": 43.0, "color": "#bc8cff"},
            {"name": "工业制造", "value": 35.5, "color": "#58a6ff"},
            {"name": "水利水务", "value": 18.2, "color": "#39c5cf"},
            {"name": "交通物流", "value": 15.0, "color": "#3fb950"},
            {"name": "医疗健康", "value": 12.0, "color": "#f0a020"},
            {"name": "农业农村", "value": 10.6, "color": "#db61a2"},
        ],
        "global_grid": [
            {"name": "城市治理", "value": 42, "unit": "%", "color": "#bc8cff", "icon": "🏙️"},
            {"name": "工厂运维", "value": 37, "unit": "%", "color": "#58a6ff", "icon": "🏭"},
            {"name": "交通/物流", "value": 31, "unit": "%", "color": "#39c5cf", "icon": "🚦"},
            {"name": "农业/能源", "value": 30, "unit": "%", "color": "#3fb950", "icon": "🌾"},
        ],
        "high_growth_verticals": [
            {"name": "石材/汽车", "rate": "CAGR 28.1%", "desc": "资产密度高、数据基础好"},
            {"name": "医疗/健康", "rate": "2025-2030 CAGR 37.7%", "desc": "药物研发、个体化医疗快速兴起"},
            {"name": "能源/电力", "rate": "CAGR 35.1%", "desc": "可再生能源场站与虚拟电厂"},
            {"name": "交通/物流", "rate": "CAGR 44.4%（C2024-2025）", "desc": "车路云一体化、低空经济"},
        ],
        "note": "城市/工业/交通是当前三大核心，合计占比超 50%",
    },

    # ---------- 5. 中国区域市场格局 ----------
    "regions": {
        "top5": [
            {"rank": 1, "name": "浙江", "share": 18.4, "tag": "第一", "color": "#bc8cff"},
            {"rank": 2, "name": "广东", "share": 17.8, "tag": "第二", "color": "#58a6ff"},
            {"rank": 3, "name": "北京", "share": 14.6, "tag": "第三", "color": "#39c5cf"},
            {"rank": 4, "name": "江苏", "share": 13.2, "tag": "第四", "color": "#3fb950"},
            {"rank": 5, "name": "上海", "share": 11.0, "tag": "第五", "color": "#f0a020"},
        ],
        "city_tiers": [
            {"name": "一线", "value": 152, "unit": "亿元", "color": "#bc8cff"},
            {"name": "新一线", "value": 168, "unit": "亿元", "color": "#58a6ff"},
            {"name": "其他", "value": 60, "unit": "亿元", "color": "#39c5cf"},
        ],
        "insight": "亚太地区占全球 42% 份额，中国是核心驱动力；环杭州湾引领长三角，粤港澳占全球 31.5%。",
        "note": "数据来源：华经 2024 / 信通院；三省一市占近 64% 市场份额",
    },

    # ---------- 6. 竞争格局：三足鼎立 ----------
    "competition": {
        "summary": "全球前五大巨头占据 54.2% 收入份额；中国市场平台型企业主导。",
        "global_leaders": ["Siemens", "Dassault Systèmes", "ANSYS", "PTC", "Autodesk", "GE Digital", "IBM", "SAP", "Microsoft"],
        "china_first_tier": ["阿里云 CIPris", "51World", "深度数智", "商汤", "软库华", "华米"],
        "segments": [
            {
                "name": "SaaS 订阅",
                "icon": "☁️",
                "desc": "商业模式、快速落地，客户粘性强。",
                "color": "#58a6ff",
            },
            {
                "name": "PaaS 平台",
                "icon": "🧩",
                "desc": "随接 AI 即插即用，生态护城河广。",
                "color": "#bc8cff",
            },
            {
                "name": "定制开发",
                "icon": "🛠️",
                "desc": "大型客户深耕，单价高、复制难。",
                "color": "#f0a020",
            },
            {
                "name": "数据接入层",
                "icon": "🔌",
                "desc": "同源数据、泛连接能力为核心，从 MES/ERP/IoT 平台向 AP 延伸。",
                "color": "#3fb950",
            },
        ],
    },

    # ---------- 7. 核心挑战与风险 ----------
    "challenges": [
        {
            "icon": "🔌",
            "title": "数据孤岛严重",
            "desc": "不同业务系统数据格式、协议差异巨大，工业现场 PLC、DCS、SCADA 互不互通。打通数据成本占项目总收入 40% 以上。",
            "color": "#f85149",
        },
        {
            "icon": "🤖",
            "title": "AI 幻觉风险",
            "desc": "生成式 AI 在工业控制场景输出必须可解释、可验证，否则会引发生产事故；人机协同与权限审计能力亟待提升。",
            "color": "#f0a020",
        },
        {
            "icon": "🔗",
            "title": "行业标准缺失",
            "desc": "不同厂商平台模型缺乏互操作，数据模型和接口定义不统一。成熟市场仍需 3-5 年，OpenUSD 等工业级标准正在加速演化。",
            "color": "#bc8cff",
        },
        {
            "icon": "👥",
            "title": "复合型人才缺口",
            "desc": "既懂工业 CT 又懂数字孪生的复合型人才稀缺，这类岗位平均薪酬较传统岗位高 40%。",
            "color": "#39c5cf",
        },
    ],

    # ---------- 8. 未来趋势展望 ----------
    "future_trends": [
        {
            "id": "01",
            "title": "云边协同成为主流架构",
            "desc": "5G + 边缘计算让实时分析和大屏渲染与 AI 决策解耦，实现\"云边端\"三级数字孪生。",
            "color": "#58a6ff",
        },
        {
            "id": "02",
            "title": "标准统一 & 生态开放",
            "desc": "OpenUSD 等工业级标准加速成熟，全球已有 20+ 国家和地区开始采用统一建模平台，降低迁移成本。",
            "color": "#bc8cff",
        },
        {
            "id": "03",
            "title": "低代码/下沉普及",
            "desc": "从\"重型定制\"转向\"可配置/可拼装\"的通用平台，技术下沉到业务人员，积木式一拖一放完成业务配置。",
            "color": "#3fb950",
        },
        {
            "id": "04",
            "title": "数字孪生 + 具身智能",
            "desc": "机器人/具身智能在物理世界执行任务前先在孪生环境中训练、仿真，孪生价值向闭环控制系统延伸。",
            "color": "#f0a020",
        },
        {
            "id": "05",
            "title": "碳达峰与数字孪生",
            "desc": "2025-2030 双碳目标加速；高能耗行业通过孪生体做能耗仿真和优化，碳管理将成长为另一类长期刚需。",
            "color": "#39c5cf",
        },
        {
            "id": "06",
            "title": "从\"决策者\"到\"运营者\"",
            "desc": "Gartner 预测 2028 年将形成数字孪生智能网络，物理世界的操作能反哺孪生模型优化，形成双向闭环。",
            "color": "#db61a2",
        },
    ],

    # ---------- 9. 行业观察信号（由 AI 每晚更新，可空） ----------
    "watch_signals": [],

    # ---------- 10. 资料来源（与参考图底部一致） ----------
    "footer_sources": [
        "国务院《数字中国建设整体布局规划》",
        "CSDN · 2025-2027 数字孪生行业深度报告",
        "可信网 · 2025 数字孪生市场运行动态报告",
        "IDC Research · Digital Twin Market Forecast 2025-2031",
        "MarketsandMarkets · Digital Twin Market Report 2031",
        "Strategic Research · Digital Twin Market Database 2032",
    ],
}

# ---------- 快照存取 ----------
_SNAPSHOT_PREFIX = "industry_overview:"


def _today_str() -> str:
    return time.strftime("%Y-%m-%d")


def get_snapshot(date_str: Optional[str] = None) -> Dict[str, Any]:
    """读取指定日期快照；不存在则返回默认快照。"""
    if date_str is None:
        date_str = _today_str()
    key = f"{_SNAPSHOT_PREFIX}{date_str}"
    row = database.query_one(f"SELECT value FROM config WHERE key = {database.PH}", (key,))
    if row and row.get("value"):
        try:
            data = json.loads(row["value"])
            data["_date"] = date_str
            data["_source"] = "db_snapshot"
            return data
        except Exception:
            pass
    # 没有当日快照，尝试取最近一份
    row = database.query_one(
        f"SELECT key, value FROM config WHERE key LIKE '{_SNAPSHOT_PREFIX}%' ORDER BY key DESC LIMIT 1"
        if not database.PG
        else f"SELECT key, value FROM config WHERE key LIKE '{_SNAPSHOT_PREFIX}%' ORDER BY key DESC LIMIT 1",
        ()
    )
    if row and row.get("value"):
        try:
            data = json.loads(row["value"])
            data["_date"] = row["key"].replace(_SNAPSHOT_PREFIX, "")
            data["_source"] = "db_snapshot_latest"
            return data
        except Exception:
            pass
    data = json.loads(json.dumps(DEFAULT_OVERVIEW, ensure_ascii=False))
    data["_date"] = date_str
    data["_source"] = "default_bundle"
    return data


def save_snapshot(data: Dict[str, Any], date_str: Optional[str] = None) -> str:
    if date_str is None:
        date_str = data.get("date") or _today_str()
    data = dict(data)
    data["date"] = date_str
    data.setdefault("generated_at", time.strftime("%Y-%m-%dT%H:%M:%S"))
    key = f"{_SNAPSHOT_PREFIX}{date_str}"
    payload = json.dumps(data, ensure_ascii=False)
    existing = database.query_one(f"SELECT value FROM config WHERE key = {database.PH}", (key,))
    if existing:
        database.execute(
            f"UPDATE config SET value = {database.PH} WHERE key = {database.PH}",
            (payload, key),
        )
    else:
        database.execute(
            f"INSERT INTO config (key, value) VALUES ({database.PH}, {database.PH})",
            (key, payload),
        )
    return date_str


def list_snapshot_dates(limit: int = 30) -> List[str]:
    rows = database.query(
        f"SELECT key FROM config WHERE key LIKE '{_SNAPSHOT_PREFIX}%' ORDER BY key DESC LIMIT {database.PH}",
        (limit,),
    )
    prefix_len = len(_SNAPSHOT_PREFIX)
    return [r["key"][prefix_len:] for r in rows if r.get("key")]


def _recent_intel(days: int = 7, limit: int = 80) -> List[Dict[str, Any]]:
    """读取近 N 天情报标题+摘要，作为 AI 前沿研判的"近期线索"（仅锚点，可为空）。"""
    try:
        start = time.strftime('%Y-%m-%d', time.localtime(time.time() - days * 86400))
        min_rel = config.MIN_RELEVANCE if hasattr(config, 'MIN_RELEVANCE') else 3
        rows = database.query(
            f'SELECT title, summary, relevance FROM intelligence WHERE date>={database.PH} ORDER BY relevance DESC, id DESC LIMIT {database.PH}',
            (start, limit),
        )
        out = []
        for r in rows:
            title = (r.get('title') or '').strip()
            if not title:
                continue
            out.append({'title': title[:120], 'summary': (r.get('summary') or '')[:200]})
        return out
    except Exception:
        return []


# 行业前沿研判的六个固定维度（立足整个行业，不局限于本站采集）
FRONTIER_DIMS: List[Dict[str, str]] = [
    {'key': 'tech', 'name': '技术前沿', 'icon': '🔬'},
    {'key': 'product', 'name': '产品平台', 'icon': '🧩'},
    {'key': 'market', 'name': '市场格局', 'icon': '📈'},
    {'key': 'policy', 'name': '政策标准', 'icon': '📜'},
    {'key': 'application', 'name': '应用落地', 'icon': '🏙️'},
    {'key': 'trend', 'name': '未来趋势', 'icon': '🔮'},
]
_SIGNAL_CATS = ('政策', '技术', '市场', '应用', '竞争', '趋势')


def build_frontier_briefing(clues: Optional[List[Dict[str, Any]]] = None) -> Optional[Dict[str, Any]]:
    """用 AI 立足【整个数字孪生/视频融合行业前沿】产出当晚的动态观察。

    - 视野不局限于本站采集：AI 依据自身对全球行业的系统认知做多维研判；
      本站近 7 天情报仅作为"近期线索"锚点（可为空）。
    - 防幻觉：趋势判断/格局分析可基于行业常识展开；但具体公司动作、融资数字、
      产品发布、中标事件等"硬事实"只能引用线索里出现过的，不得杜撰。
    返回 {'signals': [...], 'frontier': [...], 'headline': str}；AI 未配置/失败返回 None。
    """
    try:
        import ai
    except Exception:
        return None
    if not getattr(ai, 'enabled', lambda: False)():
        return None

    today = _today_str()
    clue_text = json.dumps(clues or [], ensure_ascii=False)
    prompt = (
        f'你是数字孪生/视频融合/三维可视化(CIM/BIM/GIS)/智慧城市领域的资深行业分析师。'
        f'今天是 {today}。请立足【全球整个行业的前沿动态】做当晚观察——'
        f'视野不要局限于给你的线索，要基于你对这个行业技术演进、头部厂商(如 NVIDIA Omniverse、'
        f'Siemens、Dassault、Unity、51WORLD、阿里云 CIM 等)、政策标准、市场格局、落地案例的'
        f'系统认知来研判。\n\n'
        f'下面是"本站近 7 天采集到的线索"（可能为空或很少，仅作近期事件锚点参考）：\n'
        f'{clue_text}\n\n'
        f'【事实纪律】趋势方向、技术范式、竞争格局、应用价值这类"研判"可依据行业常识充分展开；'
        f'但具体公司名+动作、融资金额、发布日期、中标金额等"硬事实"只能引用线索中出现过的，'
        f'线索里没有的硬事件不得编造（可用"头部厂商正加速布局…"这类不点名的表述）。\n\n'
        f'严格输出 JSON（不要 markdown 代码块），结构如下：\n'
        f'{{"headline":"一句话总览当晚行业风向，30~60字",\n'
        f'  "signals":[{{"category":"政策/技术/市场/应用/竞争/趋势 之一",'
        f'"text":"一条高价值观察，40~90字，覆盖多方面、不与线索强绑定"}}，...共4~6条],\n'
        f'  "frontier":[{{"dim":"tech|product|market|policy|application|trend 之一",'
        f'"title":"该维度前沿要点，≤20字","points":["要点1，40~80字","要点2","要点3"]}}，...6个维度各一个]}}\n'
        f'frontier 必须覆盖全部 6 个维度(tech技术前沿/product产品平台/market市场格局/'
        f'policy政策标准/application应用落地/trend未来趋势)，每个维度 2~3 条要点。'
        f'全部用通顺中文，可保留必要英文专有名词(如 Omniverse、OpenUSD、3DGS、Gartner)。'
    )
    try:
        raw = ai._chat([{'role': 'user', 'content': prompt}], timeout=120)
        obj = json.loads(_extract_first_json(raw))
    except Exception as exc:
        database.log('collect', '行业前沿观察 AI 生成失败，保留上一版: {}'.format(exc), 'warn')
        return None

    # 顶部信号
    signals: List[Dict[str, Any]] = []
    for s in (obj.get('signals') or []):
        if not isinstance(s, dict):
            continue
        cat = str(s.get('category') or '趋势').strip()
        if cat not in _SIGNAL_CATS:
            cat = '趋势'
        text = str(s.get('text') or '').strip()
        if 8 <= len(text) <= 200:
            signals.append({'category': cat, 'text': text[:200]})
        if len(signals) >= 6:
            break

    # 六维前沿研判：按 FRONTIER_DIMS 固定顺序建槽，再按 key 精确填充
    frontier = [{'key': d['key'], 'name': d['name'], 'icon': d['icon'],
                 'title': '', 'points': []} for d in FRONTIER_DIMS]
    slot_by_key = {x['key']: x for x in frontier}
    for f in (obj.get('frontier') or []):
        if not isinstance(f, dict):
            continue
        slot = slot_by_key.get(str(f.get('dim') or '').strip().lower())
        if not slot:
            continue
        slot['title'] = str(f.get('title') or '').strip()[:30]
        pts = []
        for p in (f.get('points') or []):
            p = str(p or '').strip()
            if 8 <= len(p) <= 200:
                pts.append(p[:200])
            if len(pts) >= 3:
                break
        slot['points'] = pts

    headline = str(obj.get('headline') or '').strip()[:120]

    if not signals and not any(f['points'] for f in frontier):
        return None
    return {'headline': headline, 'signals': signals, 'frontier': frontier}


def _extract_first_json(text: str) -> str:
    """从模型输出中截取第一个完整 JSON 对象（容忍 markdown 代码块包裹）。"""
    text = (text or '').strip()
    if text.startswith('```'):
        text = text.strip('`')
        if text[:4].lower() == 'json':
            text = text[4:]
    s = text.find('{')
    e = text.rfind('}')
    if s < 0 or e <= s:
        raise ValueError('输出中未找到 JSON 对象')
    return text[s:e + 1]


def generate_today() -> Optional[str]:
    """供 job_collect 每晚调用：生成「当天日期」的行业观察快照。

    - 以最近一份快照（无则内置 DEFAULT_OVERVIEW）为底稿，保留静态研究内容；
    - 调 build_frontier_briefing()：AI 立足整个行业前沿产出顶部信号 + 六维研判，
      本站近 7 天采集情报仅作"近期线索"锚点（可为空），故即使 0 采集也照常产出；
      AI 未配置/调用失败时保留上一版动态内容，静态内容不受影响。
    返回当天日期字符串；失败返回 None。
    """
    today = _today_str()
    try:
        # 最近一份快照（去掉 get_snapshot 附加的 _date/_source 标记）
        base = get_snapshot(today)
        base.pop('_source', None)
        base.pop('_date', None)

        clues = _recent_intel(days=7)
        briefing = build_frontier_briefing(clues)
        if briefing:
            base['watch_signals'] = briefing['signals']
            base['frontier'] = briefing['frontier']
            base['frontier_headline'] = briefing['headline']
            print('[industry_overview] 行业前沿观察已由 AI 生成：信号 {} 条、六维研判；'
                  '近期线索 {} 条'.format(len(briefing['signals']), len(clues)))
        else:
            print('[industry_overview] 本次未更新前沿观察（AI 未配置/调用失败），保留上一版')

        base['date'] = today
        base['generated_at'] = time.strftime('%Y-%m-%dT%H:%M:%S')
        save_snapshot(base, today)
        return today
    except Exception as exc:
        print(f"[industry_overview] generate_today 失败：{exc}")
        return None
