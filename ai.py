# -*- coding: utf-8 -*-
"""AI 提炼模块：调用 OpenAI 兼容接口（默认智谱 GLM-4-Flash，免费）对采集情报做摘要提炼与相关度判定。

设计要点：
- 批量请求：每批 AI_BATCH_SIZE 条合并成一次调用，控制成本与耗时
- 优雅降级：未配置 Key 或调用失败时返回 None，调用方回退到原关键词逻辑（不丢数据）
- 失败重试：网络/解析失败重试 1 次，仍失败则整批降级
"""
import json
import time

import requests

import config
import database

API_BASE = config.AI_API_BASE.rstrip('/')
API_KEY = config.AI_API_KEY
MODEL = config.AI_MODEL

SYSTEM_PROMPT = (
    '你是一名数字孪生/视频融合行业的资深情报分析师。'
    '我会给你一批采集到的资讯（标题+摘要），请逐条完成两件事：\n'
    '1. 判断该内容与「数字孪生、视频孪生、视频融合、三维可视化、三维引擎、GIS/BIM/CIM、'
    '智慧园区/智慧城市/智慧工厂等孪生类解决方案」主题的真实相关度：\n'
    '   - 只是标题碰巧含关键词、正文实际无关（如股票行情、同名公司无关业务、招聘、'
    '凑热点的营销软文等）→ keep=false, score 0-1\n'
    '   - 泛安防/物联网内容但与孪生可视化沾边 → score 2\n'
    '   - 明确涉及孪生/三维可视化产品、方案、案例、政策 → score 3-5\n'
    '2. 为每条写一段「情报速览」。硬性长度要求：每条 summary 必须 120~200 个汉字，'
    '低于 120 字视为不合格输出，会被系统拒收重新生成。\n'
    '严格按三段结构撰写，每段都要写足：\n'
    '① 核心事件（≥40字）：谁在何时何地发布/建成了什么；\n'
    '② 关键细节（≥40字）：规模、数字、技术亮点、合作伙伴、应用场景等具体信息；\n'
    '③ 行业意义（≥30字）：对数字孪生/视频融合赛道意味着什么。\n'
    '只依据标题与摘要中真实存在的信息，禁止编造数字和事实；'
    '当原文信息极少、不足120字时，必须围绕该主题补充行业背景解读（如技术应用趋势、'
    '典型场景价值、赛道竞争格局）写足120字，禁止直接缩短。\n'
    '注意：部分资讯为英文来源（如 digital twin / smart city / IoT / Omniverse 等），'
    '摘要必须用中文撰写，可保留必要的英文专有名词/产品名。\n'
    '3. title 字段：若原标题是英文（或含大段英文），翻译成简洁、通顺、符合中文科技媒体习惯的'
    '中文标题（保留 NVIDIA、Omniverse、IoT 等通用专有名词/产品名/缩写）；若原标题已是中文，'
    '则原样返回，不要改写。\n'
    '严格输出 JSON 数组（不要 markdown 代码块），每项格式：\n'
    '{"id": 编号, "keep": true/false, "score": 0-5, '
    '"title": "中文标题（英文则翻译，中文则原样）", '
    '"summary": "情报速览（120~200字）", "tags": ["最多3个，从 技术/方案/政策/竞品/案例/展会 中选"]}\n'
    '数组顺序与输入一致，一条不能少。'
)


def enabled():
    """是否已配置 AI Key"""
    return bool(API_KEY)


def _chat(messages, timeout=90):
    resp = requests.post(
        API_BASE + '/chat/completions',
        headers={'Authorization': 'Bearer ' + API_KEY,
                 'Content-Type': 'application/json'},
        json={'model': MODEL,
              'messages': messages,
              'temperature': 0.2,
              'max_tokens': 4000},
        timeout=timeout)
    resp.raise_for_status()
    return resp.json()['choices'][0]['message']['content']


def _extract_json(text):
    """从模型输出中提取 JSON 数组（容忍 markdown 代码块包裹）"""
    text = text.strip()
    if text.startswith('```'):
        text = text.strip('`')
        if text.startswith('json'):
            text = text[4:]
    s, e = text.find('['), text.rfind(']')
    if s < 0 or e <= s:
        raise ValueError('输出中未找到 JSON 数组')
    return json.loads(text[s:e + 1])


def _repair_short(items, results):
    """对摘要不足 100 字的条目做一轮补写重试（用户硬性要求：卡片内容优先体现原文核心，不短于 120 字）。
    返回修复后的 results；失败时原样返回。"""
    short_idx = [i for i, r in enumerate(results)
                 if r is not None and len((r.get('summary') or '').strip()) < 100]
    if not short_idx:
        return results
    payload = [{'id': k,
                'title': (items[k].get('title') or '')[:120],
                'draft': (results[k].get('summary') or '')[:400]}
               for k in short_idx]
    repair_prompt = (
        '下面这几条情报速览不合格：长度不足 120 个汉字。'
        '请逐条重写，硬性要求：每条 120~200 个汉字，'
        '在 draft 基础上扩写行业背景与应用价值（技术应用趋势、典型场景、赛道意义），'
        '禁止编造具体数字和事实。严格输出 JSON 数组（不要 markdown 代码块），每项格式：\n'
        '{"id": 编号, "summary": "重写后的120~200字速览"}'
    )
    try:
        raw = _chat([
            {'role': 'system', 'content': SYSTEM_PROMPT},
            {'role': 'user', 'content': json.dumps(payload, ensure_ascii=False)},
            {'role': 'assistant', 'content': repair_prompt},
            {'role': 'user', 'content': '请按上述要求重写这些条目。'},
        ])
        arr = _extract_json(raw)
        for obj in arr:
            try:
                idx = int(obj.get('id'))
            except (TypeError, ValueError):
                continue
            if idx in short_idx:
                new_sum = str(obj.get('summary') or '').strip()
                if len(new_sum) > len((results[idx].get('summary') or '').strip()):
                    results[idx]['summary'] = new_sum[:400]
    except Exception:
        pass
    return results


def analyze_batch(items):
    """批量分析情报。

    参数 items: [{'title':..., 'summary':..., 'vendor':...}, ...]
    返回: 与输入等长的列表，每项 {'keep':bool,'score':int,'title':str(中文标题/译名),
          'summary':str,'tags':[..]}；
          未配置 Key 或调用失败时返回 None（调用方降级）。
    """
    if not enabled() or not items:
        return None

    payload = []
    for i, it in enumerate(items):
        payload.append({
            'id': i,
            'title': (it.get('title') or '')[:120],
            'summary': (it.get('summary') or '')[:400],
        })

    user_prompt = json.dumps(payload, ensure_ascii=False)

    for attempt in (1, 2):
        try:
            raw = _chat([
                {'role': 'system', 'content': SYSTEM_PROMPT},
                {'role': 'user', 'content': user_prompt},
            ])
            arr = _extract_json(raw)
            results = [None] * len(items)
            for obj in arr:
                try:
                    idx = int(obj.get('id'))
                except (TypeError, ValueError):
                    continue
                if not isinstance(idx, int) or not (0 <= idx < len(items)):
                    continue
                results[idx] = {
                    'keep': bool(obj.get('keep', True)),
                    'score': max(0, min(5, int(obj.get('score', 3)))),
                    'title': str(obj.get('title') or '')[:200],
                    'summary': str(obj.get('summary') or '')[:400],
                    'tags': [str(t)[:10] for t in (obj.get('tags') or [])][:3],
                }
            if all(r is not None for r in results):
                results = _repair_short(items, results)
                return results
            # 有缺失项：缺失的用哨兵值标记（score=-1 表示无 AI 结果，调用方回退关键词逻辑）
            return [r if r is not None else {'keep': True, 'score': -1, 'title': '',
                                              'summary': '', 'tags': []} for r in results]
        except Exception as e:
            if attempt == 1:
                time.sleep(2)
            else:
                database.log('ai', 'AI 批量分析失败，本批降级为关键词逻辑: {}'.format(e), 'warn')
    return None


def test_connection():
    """连通性自检（后台「AI 设置」页用）"""
    if not enabled():
        return {'ok': False, 'msg': '未配置 AI_API_KEY'}
    try:
        raw = _chat([{'role': 'user', 'content': '回复两个字：正常'}], timeout=30)
        return {'ok': True, 'msg': '模型响应: ' + str(raw)[:50]}
    except Exception as e:
        return {'ok': False, 'msg': str(e)[:200]}
