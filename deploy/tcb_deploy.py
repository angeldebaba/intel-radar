# -*- coding: utf-8 -*-
"""
CloudBase 云托管自动部署脚本（纯标准库，无第三方依赖）

流程：
1. 自动发现/使用指定 EnvId
2. 查询服务当前运行版本，沿用其环境变量与规格配置
3. 调用 CreateCloudBaseRunServerVersion 从 GitHub 仓库构建新版本
4. 轮询构建状态直至上线成功或失败

环境变量：
  TENCENT_SECRET_ID   必填  子账号密钥ID
  TENCENT_SECRET_KEY  必填  子账号密钥Key
  TCB_SERVER_NAME     必填  云托管服务名（intel-radar 的服务为 angel）
  TCB_REGION          选填  默认 ap-shanghai
  TCB_ENV_ID          选填  环境ID，不填则自动发现
  GITHUB_REPOSITORY   选填  默认 angeldebaba/intel-radar
  GITHUB_SHA          选填  版本备注用的commit
"""
import hashlib
import hmac
import json
import os
import sys
import time
import datetime
import urllib.request
import urllib.error

SECRET_ID = os.environ.get('TENCENT_SECRET_ID', '')
SECRET_KEY = os.environ.get('TENCENT_SECRET_KEY', '')
SERVER_NAME = os.environ.get('TCB_SERVER_NAME', '')
REGION = os.environ.get('TCB_REGION', 'ap-shanghai')
ENV_ID = os.environ.get('TCB_ENV_ID', '')
REPO = os.environ.get('GITHUB_REPOSITORY', 'angeldebaba/intel-radar')
BRANCH = os.environ.get('TCB_BRANCH', 'main')
COMMIT = os.environ.get('GITHUB_SHA', '')[:8]
HOST = 'tcb.tencentcloudapi.com'
API_VERSION = '2018-06-08'

TIMEOUT_MIN = 12  # 构建轮询超时（分钟）


def die(msg, code=1):
    print('::error::' + msg)
    sys.exit(code)


def tc3_call(action, payload):
    """TC3-HMAC-SHA256 签名调用腾讯云 tcb OpenAPI"""
    ts = int(time.time())
    date = datetime.datetime.fromtimestamp(ts, datetime.timezone.utc).strftime('%Y-%m-%d')
    ct = 'application/json; charset=utf-8'
    body = json.dumps(payload)
    canonical_headers = 'content-type:%s\nhost:%s\nx-tc-action:%s\n' % (ct, HOST, action.lower())
    signed_headers = 'content-type;host;x-tc-action'
    hashed_payload = hashlib.sha256(body.encode()).hexdigest()
    canonical_request = 'POST\n/\n\n%s\n%s\n%s' % (canonical_headers, signed_headers, hashed_payload)
    algo = 'TC3-HMAC-SHA256'
    credential_scope = '%s/tcb/tc3_request' % date
    string_to_sign = '%s\n%s\n%s\n%s' % (algo, ts, credential_scope,
                                         hashlib.sha256(canonical_request.encode()).hexdigest())

    def h(key, msg):
        return hmac.new(key, msg.encode(), hashlib.sha256).digest()

    signing_key = h(h(h(('TC3' + SECRET_KEY).encode(), date), 'tcb'), 'tc3_request')
    signature = hmac.new(signing_key, string_to_sign.encode(), hashlib.sha256).hexdigest()
    authorization = '%s Credential=%s/%s, SignedHeaders=%s, Signature=%s' % (
        algo, SECRET_ID, credential_scope, signed_headers, signature)
    headers = {'Authorization': authorization, 'Content-Type': ct, 'Host': HOST,
               'X-TC-Action': action, 'X-TC-Timestamp': str(ts),
               'X-TC-Version': API_VERSION, 'X-TC-Region': REGION}
    req = urllib.request.Request('https://' + HOST, data=body.encode(), headers=headers, method='POST')
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.loads(r.read().decode()).get('Response', {})
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read().decode()).get('Response', {})
        except Exception:
            return {'Error': {'Code': 'HTTP%d' % e.code, 'Message': str(e)}}
    except Exception as e:
        return {'Error': {'Code': 'NetworkError', 'Message': repr(e)}}


def check(resp, what):
    if resp.get('Error'):
        die('%s 失败: [%s] %s' % (what, resp['Error'].get('Code'), resp['Error'].get('Message')))
    return resp


def discover_env():
    """未指定 EnvId 时自动发现（取第一个环境）"""
    resp = check(tc3_call('DescribeEnvs', {}), '查询环境列表')
    envs = resp.get('EnvList') or []
    if not envs:
        die('账号下没有云开发环境')
    env = envs[0]
    print('自动发现环境: %s (%s) 状态=%s' % (env.get('EnvId'), env.get('Alias'), env.get('Status')))
    return env['EnvId']


def get_current_version_config(env_id):
    """查询服务现有版本，返回最新版本的完整配置"""
    resp = check(tc3_call('DescribeCloudBaseRunServer',
                          {'EnvId': env_id, 'ServerName': SERVER_NAME, 'Offset': 0, 'Limit': 10}),
                 '查询云托管服务')
    items = resp.get('VersionItems') or []
    if not items:
        return None, None
    # 取默认兜底版本或最新更新版本
    active = None
    for it in items:
        if it.get('IsDefaultPriority'):
            active = it
            break
    if active is None:
        active = sorted(items, key=lambda x: x.get('UpdatedTime') or '')[-1]
    vname = active.get('VersionName')
    print('当前版本: %s 状态=%s 流量=%s' % (vname, active.get('Status'), active.get('FlowRatio')))
    detail = check(tc3_call('DescribeCloudBaseRunServerVersion',
                            {'EnvId': env_id, 'ServerName': SERVER_NAME, 'VersionName': vname}),
                   '查询版本详情')
    return vname, detail


def build_deploy_payload(env_id, cur):
    """构造部署参数：沿用现有配置，代码来源改为 GitHub 仓库"""
    p = {
        'EnvId': env_id,
        'ServerName': SERVER_NAME,
        'UploadType': 'repository',
        'RepositoryType': 'github',
        'Repository': 'https://github.com/%s' % REPO,
        'Branch': BRANCH,
        'CodeDetail': {
            'Name': {'Name': REPO.split('/')[-1], 'FullName': REPO},
            'Url': 'https://github.com/%s' % REPO,
        },
        'DockerfilePath': 'Dockerfile',
        'BuildDir': '.',
        'ContainerPort': 80,
        'FlowRatio': 100,
        'VersionRemark': 'CI deploy %s' % COMMIT if COMMIT else 'CI deploy',
    }
    # 沿用现有版本的环境变量与规格
    if cur:
        for src, dst, cast in [
            ('EnvParams', 'EnvParams', str),
            ('Cpu', 'Cpu', float),
            ('Mem', 'Mem', float),
            ('MinNum', 'MinNum', int),
            ('MaxNum', 'MaxNum', int),
            ('PolicyType', 'PolicyType', str),
            ('PolicyThreshold', 'PolicyThreshold', float),
            ('ContainerPort', 'ContainerPort', int),
        ]:
            v = cur.get(src)
            if v not in (None, ''):
                try:
                    p[dst] = cast(v)
                except (TypeError, ValueError):
                    pass
    # 规格兜底
    p.setdefault('Cpu', 0.25)
    p.setdefault('Mem', 0.5)
    p.setdefault('MinNum', 0)
    p.setdefault('MaxNum', 3)
    p.setdefault('PolicyType', 'cpu')
    p.setdefault('PolicyThreshold', 60)
    return p


def main():
    for k in ('TENCENT_SECRET_ID', 'TENCENT_SECRET_KEY', 'TCB_SERVER_NAME'):
        if not os.environ.get(k):
            die('缺少环境变量 ' + k)

    env_id = ENV_ID or discover_env()
    print('目标环境: %s 服务: %s 仓库: %s@%s' % (env_id, SERVER_NAME, REPO, BRANCH))

    cur_name, cur_detail = get_current_version_config(env_id)
    if cur_detail:
        envp = (cur_detail.get('EnvParams') or '')[:60]
        print('沿用配置: cpu=%s mem=%sG 副本=%s-%s 环境变量=%s...' % (
            cur_detail.get('Cpu'), cur_detail.get('Mem'), cur_detail.get('MinNum'),
            cur_detail.get('MaxNum'), envp))

    payload = build_deploy_payload(env_id, cur_detail)
    print('触发部署: %s' % json.dumps({k: v for k, v in payload.items() if k != 'EnvParams'},
                                      ensure_ascii=False))
    resp = check(tc3_call('CreateCloudBaseRunServerVersion', payload), '创建新版本')

    new_version = resp.get('VersionName') or ''
    run_id = resp.get('RunId') or ''
    print('新版本已创建: %s (RunId=%s)' % (new_version, run_id))
    if not new_version:
        print('返回: ' + json.dumps(resp, ensure_ascii=False)[:800])
        die('未返回版本名，请到控制台确认构建状态')

    # 轮询构建状态
    deadline = time.time() + TIMEOUT_MIN * 60
    last = ''
    while time.time() < deadline:
        time.sleep(20)
        resp = check(tc3_call('DescribeCloudBaseRunServer',
                              {'EnvId': env_id, 'ServerName': SERVER_NAME, 'Offset': 0, 'Limit': 20}),
                     '查询版本状态')
        item = None
        for it in (resp.get('VersionItems') or []):
            if it.get('VersionName') == new_version:
                item = it
                break
        if item is None:
            continue
        status = (item.get('Status') or '')
        if status != last:
            print('构建状态: %s (进度 %s%%)' % (status, item.get('Percent')))
            last = status
        low = status.lower()
        if any(k in low for k in ('normal', 'running', 'success', 'finish', 'createdone', 'deploydone')):
            print('✅ 部署成功: 版本 %s 已上线，流量 100%%' % new_version)
            return
        if any(k in low for k in ('fail', 'error', 'rollback', 'stop')):
            die('❌ 构建失败: 版本 %s 状态=%s，请到控制台查看构建日志' % (new_version, status))
    die('⏰ 构建超时（%d 分钟），请到控制台查看版本 %s 状态' % (TIMEOUT_MIN, new_version))


if __name__ == '__main__':
    main()
