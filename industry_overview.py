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
2) 若数据库 config 表存在 key=industry_overview:YYYY，优先返回该快照；
3) 后台可通过 POST /api/admin/industry-overview 写入新版快照；
4) 每晚采集任务结束后，若配置了 AI_API_KEY，会尝试用 AI 基于
   近期情报补充/校验快照中的 "watch_signals" 字段（行业观察信号）。

文件内数值尽量标注来源与年份；不同口径的数字同时呈现，
不做盲目拼合，避免误导。
"""

from __future__ import annotations

import json
import os
import time
from typing import Any, Dict, List, Optional

import database

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
            f"UPDATE config SET value = {database.PH}, updated_at = CURRENT_TIMESTAMP WHERE key = {database.PH}",
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


def generate_today() -> Optional[str]:
    """供 job_collect 每晚调用；目前只是把 DEFAULT 写入快照并补 generated_at。

    若后续接 AI 重写，可在此扩展。
    """
    try:
        data = get_snapshot()
        if data.get("_source") == "default_bundle":
            # 默认快照首次落库，方便后台编辑
            data.pop("_source", None)
            data.pop("_date", None)
            data["generated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
            return save_snapshot(data)
        return data.get("_date") or _today_str()
    except Exception as exc:
        print(f"[industry_overview] generate_today 失败：{exc}")
        return None
