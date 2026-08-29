# -*- coding: utf-8 -*-
"""
CloudBase 云托管实例数运维脚本（仅改副本配置，不触发镜像构建）

用途：
  1. 查询 angel 服务当前的最小/最大副本数、运行模式、在线 Pod 列表
  2. 将服务固定为 1 个实例（MinNum=1, MaxNum=1, OperationMode=manualScale），
     避免弹性扩容导致 APScheduler 多实例同时触发定时任务

为什么需要：
  AGENTS.md 明确要求 CloudBase 保持单实例。代码里虽有 PG 行锁防重复采集，
  但多实例仍会产生"另一实例正在推送/采集"的噪音日志，且访问统计等内存队列
  在多实例下行为不一致。固定 1 副本最省心。

用法：
  # 查询当前副本配置（只读，不改动）
  TENCENT_SECRET_ID=xxx TENCENT_SECRET_KEY=yyy \
  TCB_ENV_ID=angel-d2gws9dnv51db45e2 TCB_SERVER_NAME=angel \
      python3 deploy/tcb_scale.py --status

  # 固定为 1 个实例
  TENCENT_SECRET_ID=xxx TENCENT_SECRET_KEY=yyy \
  TCB_ENV_ID=angel-d2gws9dnv51db45e2 TCB_SERVER_NAME=angel \
      python3 deploy/tcb_scale.py --fix-one

  # 自定义副本数（一般用不到，保留以防扩容需求）
  python3 deploy/tcb_scale.py --set-min 1 --set-max 1

所需子账号策略：QcloudTCBRFullAccess

实现细节：
  - 查询用 DescribeCloudRunServerDetail（tcbr 2022-02-17）
  - 更新用 UpdateCloudRunServer，只传 Items（DiffConfigItem），DeployInfo
    必须带上但用 image 类型 + 已在线镜像占位，**不会重新构建镜像**；
    CloudBase 识别到副本配置 diff 后只做滚动扩缩容。
  - 若上面的"占位镜像更新"触发了非预期重建，可改用控制台手动改：
    云托管 → 服务 angel → 服务设置 → 副本设置 → 最小1 最大1 → 手动调节。
"""
import argparse
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
SERVER_NAME = os.environ.get('TCB_SERVER_NAME', 'angel')
REGION = os.environ.get('TCB_REGION', 'ap-shanghai')
ENV_ID = os.environ.get('TCB_ENV_ID', '')


def die(msg, code=1):
    print('[ERROR] ' + msg)
    sys.exit(code)


def tc3_call(service, host, version, action, payload):
    ts = int(time.time())
    date = datetime.datetime.fromtimestamp(ts, datetime.timezone.utc).strftime('%Y-%m-%d')
    ct = 'application/json; charset=utf-8'
    body = json.dumps(payload)
    canonical_headers = 'content-type:%s\nhost:%s\nx-tc-action:%s\n' % (
        ct, host, action.lower())
    signed_headers = 'content-type;host;x-tc-action'
    hashed_payload = hashlib.sha256(body.encode()).hexdigest()
    canonical_request = 'POST\n/\n\n%s\n%s\n%s' % (
        canonical_headers, signed_headers, hashed_payload)
    credential_scope = '%s/%s/tc3_request' % (date, service)
    string_to_sign = 'TC3-HMAC-SHA256\n%d\n%s\n%s' % (
        ts, credential_scope,
        hashlib.sha256(canonical_request.encode()).hexdigest())

    def _h(key, msg):
        return hmac.new(key, msg.encode(), hashlib.sha256).digest()

    signing_key = _h(_h(_h(('TC3' + SECRET_KEY).encode(), date), service), 'tc3_request')
    signature = hmac.new(signing_key, string_to_sign.encode(), hashlib.sha256).hexdigest()
    authorization = (
        'TC3-HMAC-SHA256 Credential=%s/%s, SignedHeaders=%s, Signature=%s'
        % (SECRET_ID, credential_scope, signed_headers, signature))
    headers = {
        'Authorization': authorization,
        'Content-Type': ct,
        'Host': host,
        'X-TC-Action': action,
        'X-TC-Timestamp': str(ts),
        'X-TC-Version': version,
        'X-TC-Region': REGION,
    }
    req = urllib.request.Request('https://' + host, data=body.encode(),
                                 headers=headers, method='POST')
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read().decode()).get('Response', {})
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read().decode()).get('Response', {})
        except Exception:
            return {'Error': {'Code': 'HTTP%d' % e.code, 'Message': str(e)}}
    except Exception as e:
        return {'Error': {'Code': 'NetworkError', 'Message': repr(e)}}


def tcbr(action, payload):
    return tc3_call('tcbr', 'tcbr.tencentcloudapi.com', '2022-02-17', action, payload)


def check_resp(resp, what):
    if resp.get('Error'):
        die('%s 失败: [%s] %s' % (
            what, resp['Error'].get('Code'), resp['Error'].get('Message')))
    return resp


def get_detail():
    """DescribeCloudRunServerDetail 返回结构里 BaseConfig 含 MinNum/MaxNum/OperationMode；
    OnlineVersionInfos 里可读到当前每个版本的 CurrentReplicas。"""
    resp = check_resp(tcbr('DescribeCloudRunServerDetail', {
        'EnvId': ENV_ID, 'ServerName': SERVER_NAME,
    }), '查询服务详情')
    detail = resp.get('BaseConfig') or resp.get('ServerConfig') or {}
    versions = resp.get('OnlineVersionInfos') or resp.get('VersionInfos') or []
    pods = resp.get('PodList') or resp.get('Pods') or []
    return {
        'min_num': detail.get('MinNum'),
        'max_num': detail.get('MaxNum'),
        'op_mode': detail.get('OperationMode'),
        'cpu': detail.get('Cpu'),
        'mem': detail.get('Mem'),
        'port': detail.get('Port'),
        'access_types': detail.get('OpenAccessTypes') or detail.get('AccessTypes'),
        'status': (resp.get('ServerInfo') or {}).get('Status'),
        'online_versions': versions,
        'pods': pods,
        'raw_detail': detail,
    }


def print_status(info):
    print('=== CloudBase 服务 %s (env=%s, region=%s) ===' % (
        SERVER_NAME, ENV_ID, REGION))
    print('服务状态        : %s' % info.get('status'))
    print('最小副本 MinNum : %s' % info.get('min_num'))
    print('最大副本 MaxNum : %s' % info.get('max_num'))
    print('运行模式        : %s  (noScale=无弹性 / condScale=按指标 / '
          'manualScale=手动固定 / alwaysScale=始终弹性)' % info.get('op_mode'))
    print('CPU/内存        : %s 核 / %s GB' % (info.get('cpu'), info.get('mem')))
    print('服务端口        : %s' % info.get('port'))
    print('公网访问类型    : %s' % info.get('access_types'))
    versions = info.get('online_versions') or []
    if versions:
        print('在线版本:')
        for v in versions:
            print('  - %-20s 流量=%s%% 当前副本=%s 最大副本=%s' % (
                v.get('VersionName'),
                v.get('FlowRatio'),
                v.get('CurrentReplicas'),
                v.get('MaxReplicas')))
    pods = info.get('pods') or []
    if pods:
        print('在线 Pod 实例 (当前真实运行数=%d):' % len(pods))
        for p in pods:
            print('  - %s  status=%s  created=%s' % (
                p.get('PodId'), p.get('Status'), p.get('CreateTime')))
    else:
        print('在线 Pod 实例: 接口未返回明细（不同版本字段可能有差异）')


def pick_online_image(info):
    """从详情里挑一个在线版本的镜像地址，用于更新调用时占位。
    UpdateCloudRunServer 必须传 DeployInfo，传已在线镜像可避免触发重新构建。"""
    for v in info.get('online_versions') or []:
        img = v.get('ImageUrl')
        if img:
            return img
    # 兜底：不带镜像，只改 Items；部分 CloudBase 版本允许 DeployType=image + 空 ImageUrl
    return ''


def update_replicas(min_num, max_num, op_mode='manualScale'):
    info = get_detail()
    image_url = pick_online_image(info)
    items = [
        {'Key': 'MinNum', 'IntValue': int(min_num)},
        {'Key': 'MaxNum', 'IntValue': int(max_num)},
        {'Key': 'OperationMode', 'Value': op_mode},
    ]
    deploy_info = {
        'DeployType': 'image',
        'ReleaseType': 'FULL',
        'DeployRemark': 'scale-to-%d via tcb_scale.py' % max_num,
    }
    if image_url:
        deploy_info['ImageUrl'] = image_url
    payload = {
        'EnvId': ENV_ID,
        'ServerName': SERVER_NAME,
        'DeployInfo': deploy_info,
        'Items': items,
    }
    print('即将提交副本配置变更:')
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    resp = check_resp(tcbr('UpdateCloudRunServer', payload), '更新副本配置')
    print('[OK] 已提交: TaskId=%s RequestId=%s' % (
        resp.get('TaskId'), resp.get('RequestId')))
    print('等待 15 秒后重新查询...')
    time.sleep(15)
    new_info = get_detail()
    print_status(new_info)
    # 校验是否生效
    if new_info.get('min_num') == min_num and new_info.get('max_num') == max_num:
        print('[OK] 副本配置已生效: MinNum=%s MaxNum=%s' % (
            new_info.get('min_num'), new_info.get('max_num')))
    else:
        print('[WARN] 接口已返回但数值尚未刷新，请 1-2 分钟后再运行 --status 确认；'
              '若长期未变请到控制台手动检查。')


def main():
    if not SECRET_ID or not SECRET_KEY:
        die('缺少环境变量 TENCENT_SECRET_ID / TENCENT_SECRET_KEY')
    if not ENV_ID:
        die('缺少环境变量 TCB_ENV_ID（示例: angel-d2gws9dnv51db45e2）')

    parser = argparse.ArgumentParser(
        description='CloudBase 云托管 angel 服务副本运维')
    parser.add_argument('--status', action='store_true',
                        help='只查询当前副本配置（默认行为）')
    parser.add_argument('--fix-one', action='store_true',
                        help='固定为 1 个实例（MinNum=1 MaxNum=1 manualScale）')
    parser.add_argument('--set-min', type=int, default=None,
                        help='自定义最小副本数')
    parser.add_argument('--set-max', type=int, default=None,
                        help='自定义最大副本数')
    parser.add_argument('--op-mode', default='manualScale',
                        choices=['noScale', 'condScale', 'alwaysScale',
                                 'custom', 'manualScale'],
                        help='运行模式，默认 manualScale')
    args = parser.parse_args()

    if args.fix_one:
        update_replicas(1, 1, 'manualScale')
        return
    if args.set_min is not None or args.set_max is not None:
        cur = get_detail()
        mn = args.set_min if args.set_min is not None else cur.get('min_num') or 1
        mx = args.set_max if args.set_max is not None else cur.get('max_num') or mn
        if mx < mn:
            die('最大副本数(%s)不能小于最小副本数(%s)' % (mx, mn))
        update_replicas(mn, mx, args.op_mode)
        return

    # 默认：查询
    info = get_detail()
    print_status(info)


if __name__ == '__main__':
    main()
