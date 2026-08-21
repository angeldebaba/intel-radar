# -*- coding: utf-8 -*-
"""行业情报雷达 - 独立推送CLI（供 WorkBuddy 自动化调用，不依赖 Web 站点）
用法:
    python pushplus_cli.py "标题" "内容文件路径"
    python pushplus_cli.py --token <token> "标题" "内容文件路径"
内容文件为 HTML 或纯文本。
"""
import os
import sys
import json
import requests

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
TOKEN_FILE = os.path.join(BASE_DIR, 'pushplus_token.txt')
PUSH_URL = 'http://www.pushplus.plus/send'


def get_token(cli_token=None):
    if cli_token:
        return cli_token
    if os.environ.get('PUSHPLUS_TOKEN'):
        return os.environ['PUSHPLUS_TOKEN']
    if os.path.exists(TOKEN_FILE):
        return open(TOKEN_FILE, 'r', encoding='utf-8').read().strip()
    return ''


def send(title, content):
    token = get_token()
    if not token:
        print('ERROR: 未配置PushPlus Token。请先运行: python pushplus_setup.py <token>')
        return False
    try:
        resp = requests.post(PUSH_URL, json={
            'token': token,
            'title': title[:50],
            'content': content[:20000],
            'template': 'html',
        }, timeout=20)
        data = resp.json()
        if data.get('code') == 200:
            print('OK: 推送成功 ->', data.get('msg'))
            return True
        print('ERROR: 推送失败 ->', data.get('msg', resp.text[:200]))
        return False
    except Exception as e:
        print('ERROR: 推送异常 ->', e)
        return False


def main():
    args = sys.argv[1:]
    token = None
    if args and args[0] == '--token':
        token = args[1]
        args = args[2:]
    if len(args) < 2:
        print('用法: python pushplus_cli.py [--token <token>] "标题" "内容文件路径"')
        sys.exit(1)
    title, content_path = args[0], args[1]
    if not os.path.exists(content_path):
        # 内容直接作为字符串传入
        content = content_path
    else:
        with open(content_path, 'r', encoding='utf-8') as f:
            content = f.read()
    ok = send(title, content)
    sys.exit(0 if ok else 1)


if __name__ == '__main__':
    main()
