# -*- coding: utf-8 -*-
"""行业情报雷达 - 保存 PushPlus Token（一次性设置）
用法: python pushplus_setup.py <你的token>
"""
import os
import sys

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
TOKEN_FILE = os.path.join(BASE_DIR, 'pushplus_token.txt')


def main():
    if len(sys.argv) < 2:
        print('用法: python pushplus_setup.py <你的PushPlus Token>')
        print('获取方式: 手机打开 https://www.pushplus.plus/ 微信扫码登录，关注公众号后复制token')
        sys.exit(1)
    token = sys.argv[1].strip()
    if not token:
        print('Token不能为空')
        sys.exit(1)
    with open(TOKEN_FILE, 'w', encoding='utf-8') as f:
        f.write(token)
    print('OK: Token已保存到', TOKEN_FILE)
    print('验证推送: python pushplus_cli.py "测试" "你好，这是行业情报雷达的测试消息"')


if __name__ == '__main__':
    main()
