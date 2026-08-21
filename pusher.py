# -*- coding: utf-8 -*-
"""行业情报雷达 - 微信推送（PushPlus 免费通道）"""
import json

import requests

import config
import database


def send_push(title, content, template='html'):
    """通过 PushPlus 推送消息到微信。
    使用方式：在 https://www.pushplus.plus/ 微信扫码登录，
    关注「pushplus推送加」公众号，复制 token，填到后台设置。
    返回 (ok, message)
    """
    token = config.PUSHPLUS_TOKEN or database.get_config('pushplus_token', '')
    if not token:
        return False, '未配置 PushPlus Token（后台-推送设置 中填写）'
    try:
        resp = requests.post(config.PUSHPLUS_URL, json={
            'token': token,
            'title': title,
            'content': content,
            'template': template,
        }, timeout=15)
        data = resp.json()
        if data.get('code') == 200:
            return True, '推送成功'
        return False, '推送失败: {}'.format(data.get('msg', resp.text[:100]))
    except Exception as e:
        return False, '推送异常: {}'.format(e)


def build_daily_content(items, max_items=config.PUSH_TOP_N):
    """构造日报 HTML 内容（卡片式）"""
    if not items:
        return '<p>今日暂无高相关度情报。</p>'
    cards = []
    for i, it in enumerate(items, 1):
        stars = '★' * it['relevance'] + '☆' * (5 - it['relevance'])
        vendor = it['vendor'] or '行业动态'
        industry = it['industry'] or '数字孪生'
        tag_html = ' '.join('<span style="background:#2b4d6f;color:#cfe8ff;padding:1px 8px;'
                            'border-radius:8px;font-size:12px;margin-right:4px;">{}</span>'
                            .format(t) for t in json.loads(it['tags'] or '[]'))
        summary = (it['summary'] or it['title'] or '')[:120]
        cards.append('''
<div style="border:1px solid #444;border-left:4px solid #f0a020;border-radius:6px;
     padding:10px 14px;margin:8px 0;background:#222;">
  <div style="font-size:12px;color:#f0a020;">#{i} · 相关度 {stars} · {vendor} · {industry}</div>
  <div style="font-size:15px;font-weight:bold;margin:6px 0;color:#eee;">{title}</div>
  <div style="font-size:13px;color:#bbb;margin:4px 0;">{summary}</div>
  <div style="font-size:12px;margin:4px 0;">{tags}</div>
  <a href="{url}" style="color:#58a6ff;font-size:13px;">阅读原文 ↗</a>
</div>'''.format(
            i=i, stars=stars, vendor=vendor, industry=industry,
            title=it['title'], summary=summary, tags=tag_html, url=it['url']))
    return ''.join(cards)


def push_daily_top():
    """推送今天相关度最高的 N 条到微信"""
    items = database.query(
        'SELECT * FROM intelligence WHERE date=? AND relevance>=3 '
        'ORDER BY relevance DESC, id DESC LIMIT ?',
        (database.today_str(), config.PUSH_TOP_N))
    if not items:
        # 无高相关，放宽到当天全部
        items = database.query(
            'SELECT * FROM intelligence WHERE date=? ORDER BY relevance DESC, id DESC LIMIT ?',
            (database.today_str(), config.PUSH_TOP_N))
    if not items:
        database.log('push', '今天暂无情报，未推送')
        return False, '今天暂无情报'
    content = build_daily_content(items)
    ok, msg = send_push('📡 行业情报雷达日报 {}'.format(database.today_str()), content)
    database.log('push', '推送 {} 条: {}'.format(len(items), msg), 'ok' if ok else 'error')
    return ok, msg


def push_manual(items=None):
    """手动测试推送（后台按钮）"""
    if items is None:
        items = database.query(
            'SELECT * FROM intelligence ORDER BY relevance DESC, id DESC LIMIT ?',
            (config.PUSH_TOP_N,))
    content = build_daily_content(items)
    ok, msg = send_push('🔔 测试推送 {}'.format(database.today_str()), content)
    return ok, msg


if __name__ == '__main__':
    database.init_db()
    ok, msg = push_daily_top()
    print(msg)
