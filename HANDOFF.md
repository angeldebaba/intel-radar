# 🔄 HANDOFF — 项目现场快照（2026-08-22）

> 换电脑 / 换环境恢复现场用。配合 README.md 阅读：README 讲怎么跑，本文件讲"现在进行到哪了"。

## 一、当前状态（一句话）

功能全部完成并已上线验证：**采集 → AI 提炼/过滤 → 卡片展示 → 定时任务 → 微信推送** 全链路跑通，云端 Render + 本地均可运行。

## 二、最近完成的工作（按时间）

1. 修复致命 bug：`collector.py` 引用不存在的 `config.OFFICIAL_CONFIG`（曾导致云端定时采集每晚崩溃）
2. 修复后台未鉴权漏洞：「⚙ 后台」链接改为先跳登录页
3. UX 三连改：筛选栏单行化 / 趋势仪表盘移入后台「数据概览」/ 卡片放大 + 分页 + 缩略图（og:image 抓取）
4. **AI 提炼与智能过滤（核心新功能）**：
   - 新增 `ai.py`：批量调用 OpenAI 兼容接口，对每批 10 条情报做「摘要提炼 + 相关度 0-5 打分 + 打标签」
   - 低于 `AI_MIN_SCORE`（默认 2 分）的内容自动丢弃，实测能准确丢掉股票行情页、获奖软文、无关产品页
   - 无 Key / API 失败时优雅降级回关键词逻辑，采集永不中断
   - 默认服务商：**智谱 GLM-4-Flash（免费）**，可换 DeepSeek 等任意 OpenAI 兼容服务
   - 前端卡片展示 AI 摘要（过长收起可展开），点标题跳原文

## 三、环境变量清单（名称，值不进仓库！）

| 变量 | 说明 | 云端 Render |
|---|---|---|
| `ADMIN_PASSWORD` | 后台登录密码 | ✅ 已配置 |
| `SECRET_KEY` | Flask 会话密钥 | ✅ 已配置 |
| `PUSHPLUS_TOKEN` | 微信推送 token（pushplus.plus） | ✅ 已配置 |
| `AI_API_KEY` | 智谱 Key（open.bigmodel.cn，glm-4-flash 免费） | ✅ 已配置 |
| `AI_API_BASE` | 默认 `https://open.bigmodel.cn/api/paas/v4`，可覆盖 | 默认值即可 |
| `AI_MODEL` | 默认 `glm-4-flash`，可覆盖 | 默认值即可 |
| `DATA_DIR` | 本地运行时 SQLite 目录 | Render 自动挂 |
| `BACKUP_DIR` | 对象存储备份目录（可选） | ✅ 已配置 |

⚠️ **注意**：改 Render 环境变量后需手动 Manual Deploy 才会生效。

## 四、新电脑恢复现场（10 分钟）

```bash
# 1. 拉代码（无 git 时直接下 zip 解压）
#    https://github.com/angeldebaba/intel-radar/archive/refs/heads/main.zip

# 2. 建虚拟环境 + 装依赖（Python 3.12 验证通过）
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
#   若 APScheduler 报时区错误：pip install tzdata

# 3. 启动（本地测试）
DATA_DIR=./data AI_API_KEY=你的key .venv/bin/python app.py
#   访问 http://127.0.0.1:5000

# 4. 验证 AI 通道：登录后台 → AI 设置 → 测试连接，应返回"模型响应: 正常"
```

本机不可迁移的内容（换机即失效，需要重建）：
- `data/radar.db`：本地测试数据库（云端数据在 Render 磁盘上，独立）
- `.venv`：虚拟环境，重装即可
- 环境变量的值：去 Render 控制台 / 智谱控制台查看

## 五、待办 / 已知问题

- [ ] 官网采集源部分失效（超图/数字冰雹/51WORLD 官网 404/403），可在 collector.py 的 OFFICIAL_CONFIG 中更新 URL
- [ ] README 中 DeepSeek 相关描述可更新为智谱（config 注释已更新）
- [ ] 定期检查智谱免费额度政策是否变化

## 六、协作约定

- 本机无 git 时，用 GitHub Contents API（PUT 单文件 + PAT）推送，脚本临时写临时删，token 不落盘
- 数据库 schema 变更走 `PRAGMA table_info` 检测 + `ALTER TABLE` 自动迁移，老库无破坏
