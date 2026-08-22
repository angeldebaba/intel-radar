# -*- coding: utf-8 -*-
"""
CloudBase 云托管自动部署脚本（tcbr 新版接口，纯标准库）

流程：
1. 自动发现/使用指定 EnvId
2. 调用 tcbr UpdateCloudRunServer 从 GitHub 仓库触发构建部署（沿用服务现有配置）
3. 轮询 DescribeCloudRunDeployRecord 直至部署成功或失败

所需子账号策略：QcloudTCBFullAccess（环境发现）+ QcloudTCBRFullAccess（部署）

环境变量：
  TENCENT_SECRET_ID   必填  子账号密钥ID
  TENCENT_SECRET_KEY  必填  子账号密钥Key
  TCB_SERVER_NAME     必填  云托管服务名（intel-radar 的服务为 angel）
  TCB_REGION          选填  默认 ap-shanghai
  TCB_ENV_ID          选填  环境ID，不填则自动发现
  GITHUB_REPOSITORY   选填  默认 angeldebaba/intel-radar
  TCB_BRANCH          选填  默认 main
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

TIMEOUT_MIN = 12  # 构建轮询超时（分钟）


def die(msg, code=1):
    print('::error::' + msg)
    sys.exit(code)


def tc3_call(service, host, version, action, payload):
    """TC3-HMAC-SHA256 签名调用腾讯云 OpenAPI"""
    ts = int(time.time())
    date = datetime.datetime.fromtimestamp(ts, datetime.timezone.utc).strftime('%Y-%m-%d')
    ct = 'application/json; charset=utf-8'
    body = json.dumps(payload)
    canonical_headers = 'content-type:%s\nhost:%s\nx-tc-action:%s\n' % (ct, host, action.lower())
    signed_headers = 'content-type;host;x-tc-action'
    hashed_payload = hashlib.sha256(body.encode()).hexdigest()
    canonical_request = 'POST\n/\n\n%s\n%s\n%s' % (canonical_headers, signed_headers, hashed_payload)
    algo = 'TC3-HMAC-SHA256'
    credential_scope = '%s/%s/tc3_request' % (date, service)
    string_to_sign = '%s\n%s\n%s\n%s' % (algo, ts, credential_scope,
                                         hashlib.sha256(canonical_request.encode()).hexdigest())

    def h(key, msg):
        return hmac.new(key, msg.encode(), hashlib.sha256).digest()

    signing_key = h(h(h(('TC3' + SECRET_KEY).encode(), date), service), 'tc3_request')
    signature = hmac.new(signing_key, string_to_sign.encode(), hashlib.sha256).hexdigest()
    authorization = '%s Credential=%s/%s, SignedHeaders=%s, Signature=%s' % (
        algo, SECRET_ID, credential_scope, signed_headers, signature)
    headers = {'Authorization': authorization, 'Content-Type': ct, 'Host': host,
               'X-TC-Action': action, 'X-TC-Timestamp': str(ts),
               'X-TC-Version': version, 'X-TC-Region': REGION}
    req = urllib.request.Request('https://' + host, data=body.encode(), headers=headers, method='POST')
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


def tcb(action, payload):
    return tc3_call('tcb', 'tcb.tencentcloudapi.com', '2018-06-08', action, payload)


def tcbr(action, payload):
    return tc3_call('tcbr', 'tcbr.tencentcloudapi.com', '2022-02-17', action, payload)


def check(resp, what):
    if resp.get('Error'):
        die('%s 失败: [%s] %s' % (what, resp['Error'].get('Code'), resp['Error'].get('Message')))
    return resp


def discover_env():
    """未指定 EnvId 时自动发现（取第一个环境）"""
    resp = check(tcb('DescribeEnvs', {}), '查询环境列表')
    envs = resp.get('EnvList') or []
    if not envs:
        die('账号下没有云开发环境')
    env = envs[0]
    print('自动发现环境: %s (%s) 状态=%s' % (env.get('EnvId'), env.get('Alias'), env.get('Status')))
    return env['EnvId']


def main():
    for k in ('TENCENT_SECRET_ID', 'TENCENT_SECRET_KEY', 'TCB_SERVER_NAME'):
        if not os.environ.get(k):
            die('缺少环境变量 ' + k)

    env_id = ENV_ID or discover_env()
    remark = ('CI deploy %s' % COMMIT) if COMMIT else 'CI deploy'
    print('目标环境: %s 服务: %s 仓库: %s@%s' % (env_id, SERVER_NAME, REPO, BRANCH))

    payload = {
        'EnvId': env_id,
        'ServerName': SERVER_NAME,
        'DeployInfo': {
            'DeployType': 'repository',
            'RepoInfo': {'Source': 'github', 'Repo': REPO, 'Branch': BRANCH},
            'ReleaseType': 'FULL',
            'DeployRemark': remark,
        },
    }
    print('触发部署: %s' % json.dumps(payload, ensure_ascii=False))
    resp = check(tcbr('UpdateCloudRunServer', payload), '触发部署')

    print('部署任务已提交: %s' % json.dumps(resp, ensure_ascii=False)[:300])

    # 轮询部署记录
    deadline = time.time() + TIMEOUT_MIN * 60
    last = ''
    while time.time() < deadline:
        time.sleep(20)
        resp = tcbr('DescribeCloudRunDeployRecord',
                    {'EnvId': env_id, 'ServerName': SERVER_NAME})
        if resp.get('Error'):
            print('查询部署记录失败: %s（继续轮询）' % resp['Error'].get('Message'))
            continue
        records = resp.get('DeployRecords') or []
        if not records:
            continue
        rec = records[0]
        status = rec.get('Status') or ''
        if status != last:
            print('部署状态: %s 部署Id=%s 时间=%s' % (status, rec.get('DeployId'), rec.get('DeployTime')))
            last = status
        low = status.lower()
        if 'fail' in low or 'error' in low or 'rollback' in low or 'stop' in low:
            die('❌ 部署失败: 状态=%s，请到控制台查看构建日志（RunId=%s）' % (status, rec.get('RunId')))
        if 'success' in low or 'done' in low or 'finish' in low or 'normal' in low or 'released' in low:
            print('✅ 部署成功: 版本已全量上线')
            return
    die('⏰ 部署超时（%d 分钟），请到控制台查看服务 %s 状态' % (TIMEOUT_MIN, SERVER_NAME))


if __name__ == '__main__':
    main()
