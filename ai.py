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
    '1. 判断该内容与「数字孪生、视频融合、三维可视化、三维引擎、GIS/BIM/CIM、'
    '智慧园区/智慧城市/智慧工厂等孪生类解决方案」主题的真实相关度：\n'
    '   - 只是标题碰巧含关键词、正文实际无关（如股票行情、同名公司无关业务、招聘、'
    '凑热点的营销软文等）→ keep=false, score 0-1\n'
    '   - 泛安防/物联网内容但与孪生可视化沾边 → score 2\n'
    '   - 明确涉及孪生/三维可视化产品、方案、案例、政策 → score 3-5\n'
    '2. 为每条写一段 120~200 个汉字的「情报速览」，让读者不用点开原文就能了解全貌。'
    '结构：①核心事件（谁发布/建成了什么，何时何地）；②关键细节（规模、数字、'
    '技术亮点、合作伙伴、应用场景）；③行业意义（对数字孪生/视频融合赛道意味着什么，一句话）。'
    '只依据标题与摘要中真实存在的信息，禁止编造数字和事实；信息不足时可基于标题合理概述，'
    '原文信息极少时也要写足 120 字的行业背景解读。\n'
    '严格输出 JSON 数组（不要 markdown 代码块），每项格式：\n'
    '{"id": 编号, "keep": true/false, "score": 0-5, '
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


def analyze_batch(items):
    """批量分析情报。

    参数 items: [{'title':..., 'summary':..., 'vendor':...}, ...]
    返回: 与输入等长的列表，每项 {'keep':bool,'score':int,'summary':str,'tags':[..]}；
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
                    'summary': str(obj.get('summary') or '')[:400],
                    'tags': [str(t)[:10] for t in (obj.get('tags') or [])][:3],
                }
            if all(r is not None for r in results):
                return results
            # 有缺失项：缺失的用哨兵值标记（score=-1 表示无 AI 结果，调用方回退关键词逻辑）
            return [r if r is not None else {'keep': True, 'score': -1, 'summary': '',
                                              'tags': []} for r in results]
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
