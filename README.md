# 📡 行业情报雷达（数字孪生 · 视频融合）

面向数字孪生/视频融合产品经理的行业情报平台：**每天自动采集厂商动态 → AI 提炼摘要 → 微信推送 Top3 → 导入自有功能 → 自动生成竞品分析报告**。

> 🧑‍💻 开发者/二次维护请先读 [AGENTS.md](AGENTS.md)（代码结构、排障路径、协作约定）；
> 🔄 当前进度与部署现场见 [HANDOFF.md](HANDOFF.md)。

## 功能总览

| 模块 | 说明 |
|---|---|
| **情报日报流** | 按日期查看每日采集的行业情报，支持厂商/行业/标签/相关度/关键词筛选、相关度/最新双排序，PC 与手机自适应 |
| **每日关注** | 公开页五维度（行业动态/产品/技术/市场/关注点）当日情报聚合，每晚采集后自动生成快照 |
| **趋势仪表盘** | 近 7 日采集趋势图、厂商情报分布 Top12、标签云，一览行业热度（后台「数据概览」） |
| **每日自动采集** | 后端调度器定时从厂商官网 + 搜狗/必应/百度按「厂商 × 行业」组合抓取，自动打标签、评相关度、全局去重 |
| **AI 提炼过滤** | 智谱 GLM-4-Flash（免费）批量生成 120~200 字三段式摘要 + 0~5 分相关度打分，低于阈值自动丢弃；失败降级关键词逻辑，采集不中断 |
| **质量门禁** | 自动识别"仅含链接无实质文本"的低质条目（首页/下载站/导航堆砌），进隔离区备查，后台可审查/深抓复检/恢复入库 |
| **全文存档** | 原文 HTML/纯文本本地存档，`/article/<id>` 可回看，避免链接过期/反爬导致情报丢失 |
| **微信推送** | 每天早 8 点把相关度最高的 3 条推送到微信（**PushPlus 免费方案**） |
| **后台管理** | 密码登录（前台不展示入口），可导入大华功能清单、生成竞品分析报告、手动采集/推送、配置定时、查看运行日志与隔离区 |
| **竞品分析** | 自动比对「大华功能清单 vs 采集情报」，输出潜在差距项、厂商覆盖度、行业热点功能，支持一键导出 HTML 报告 |
| **访问统计** | 埋点 + 会话时长 + 来源追踪 + IP 归属地富化，后台可看明细（90 天滚动） |

## 已覆盖厂商（12 家，可扩展）

海康威视、智汇云舟、51WORLD、优锘科技、大华股份、华为、腾讯云、阿里云、超图软件、数字冰雹、商汤科技、百度智能云

其中 9 家有官方新闻页直采（见 `collector.py` 的 `OFFICIAL_CONFIG`），其余走搜索引擎组合查询。

## 已覆盖行业

智慧医院、智慧校园、建筑、景区、园区（全部限定数字孪生/视频融合/三维可视化主题）

---

## 快速开始（本地运行）

```bash
# 1. 安装依赖（Python 3.11 / 3.12 均可）
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
# APScheduler 若报 tzdata 缺失：pip install tzdata

# 2. 启动（Windows 也可直接双击 run.bat，首次需改里面的 Python 路径）
DATA_DIR=./data python app.py
# 没有 AI_API_KEY 也能跑：自动降级到关键词评分；要体验 AI 提炼见下方环境变量

# 3. 访问
#    前台: http://127.0.0.1:5000
#    后台: http://127.0.0.1:5000/#admin   （默认密码 luban2026，可通过 ADMIN_PASSWORD 覆盖）
```

### 关键环境变量（本地可选，线上必填）

| 变量 | 说明 | 默认值 |
|---|---|---|
| `ADMIN_PASSWORD` | 后台登录密码（**生产必须改**） | `luban2026` |
| `SECRET_KEY` | Flask session 密钥 | `intel-radar-secret-2026` |
| `AI_API_KEY` | 智谱 API Key（`open.bigmodel.cn`，glm-4-flash 免费） | 空（降级关键词评分） |
| `AI_API_BASE` | OpenAI 兼容 base URL（可换 DeepSeek 等） | `https://open.bigmodel.cn/api/paas/v4` |
| `AI_MODEL` | 模型名 | `glm-4-flash` |
| `AI_MIN_SCORE` | AI 评分低于此值丢弃 | `3` |
| `PUSHPLUS_TOKEN` | PushPlus 微信推送 token | 空 |
| `DATA_DIR` | SQLite 数据库目录 | `/tmp/radar_data` |
| `BACKUP_DIR` | 备份目录（对象存储挂载点，容器重启可恢复） | 空 |
| `COLLECT_TIME` | 每日采集时间（HH:MM） | `23:00` |
| `PUSH_TIME` | 每日推送时间（HH:MM） | `08:00` |
| `DAILY_FOCUS_TIME` | 每日关注快照生成时间 | `23:30` |
| `FRESH_DAYS` | 文章发布时间超过 N 天不入库 | `30` |
| `INTEL_RETENTION_DAYS` | 情报保留天数 | `90` |
| `VISIT_RETENTION_DAYS` | 访问明细保留天数 | `90` |
| `QUARANTINE_ENABLED` | 质量门禁开关（`1`/`0`） | `1` |
| `DATABASE_URL` | 若设置则使用 PostgreSQL（否则 SQLite） | 空 |

完整列表见 `config.py`。本地测试只需 `DATA_DIR=./data`，其他按需。

---

## 云端部署

### 方案 A：腾讯云 CloudBase 云托管（当前生产方案，推荐）

项目已内置 `Dockerfile` 和 `.github/workflows/deploy.yml`，**push 到 main 即自动部署**。

**首次接入**：

1. 登录 [腾讯云](https://cloud.tencent.com/)（完成实名认证），进入 [云开发 CloudBase 控制台](https://console.cloud.tencent.com/tcb)
2. 创建环境（按量计费，有免费额度），进入 **云托管 → 新建服务**：
   - 来源选 **GitHub 仓库**（首次授权 CloudBase 访问 GitHub）
   - 仓库：`angeldebaba/intel-radar`，分支 `main`
   - 构建：自动识别 `Dockerfile`
   - 端口：`80`
3. **持久化存储**（关键，否则重启丢数据）：
   - 服务详情 → **存储** → 新建并挂载**云硬盘**，挂载路径 `/data`
   - 在服务**环境变量**里设 `DATA_DIR=/data`
   - （可选）再挂对象存储到某路径（如 `/mnt/backup`），设 `BACKUP_DIR=/mnt/backup`
4. 配置环境变量（上一节表中**生产必须**的几项，至少 `ADMIN_PASSWORD` / `SECRET_KEY` / `DATA_DIR`）
5. 服务详情里设**实例数 = 1**（代码有跨实例防重跑锁，但单实例最省心）
6. 访问 `https://<服务域名>.tcloudbaseapp.com`

**配置 GitHub Actions 自动发布**（可选，已在仓库内写好 workflow）：

在 GitHub 仓库 Settings → Secrets 添加：

| Secret | 说明 |
|---|---|
| `TENCENT_SECRET_ID` | 子账号 SecretId（授予 `QcloudTCBFullAccess` + `QcloudTCBRFullAccess`） |
| `TENCENT_SECRET_KEY` | 对应 SecretKey |

之后每次 push 到 `main` 会自动触发 `deploy/tcb_deploy.py` 调腾讯云 OpenAPI 重新构建发布（并发组 `deploy-production` 排队不取消）。

### 方案 B：自有 VPS（最可控）

```bash
apt install python3-pip
git clone https://github.com/angeldebaba/intel-radar.git && cd intel-radar
pip install -r requirements.txt

# 方式 1：直接跑（简单）
ADMIN_PASSWORD=xxx PUSHPLUS_TOKEN=xxx AI_API_KEY=xxx DATA_DIR=/var/lib/radar \
  nohup python app.py > radar.log 2>&1 &

# 方式 2：systemd 守护（推荐，自动重启）
cat > /etc/systemd/system/intel-radar.service <<'EOF'
[Unit]
Description=Intel Radar
After=network.target

[Service]
WorkingDirectory=/opt/intel-radar
Environment=ADMIN_PASSWORD=xxx
Environment=DATA_DIR=/var/lib/radar
# 其余环境变量写在这里或用 EnvironmentFile
ExecStart=/usr/bin/python3 app.py
Restart=always

[Install]
WantedBy=multi-user.target
EOF

systemctl enable --now intel-radar
```

> 服务器在中国大陆：搜狗/百度等数据源直连即可；在海外也兼容，不影响主链路。

---

## 微信推送配置（PushPlus，完全免费）

1. 手机打开 <https://www.pushplus.plus/>，微信扫码登录
2. 按引导关注公众号「**pushplus推送加**」
3. 复制页面上的 **token**
4. 打开本系统后台 → **采集推送** 页签 → 粘贴 token → 保存 → 点「测试推送」验证
5. 每天 `PUSH_TIME`（默认早 8 点）会收到「📡 行业情报雷达日报」

> 备选：Server 酱（sct.ftqq.com）。本项目按 PushPlus 实现，要改通道请改 `pusher.send_push()`。

---

## 后台使用说明

进入方式：前台右上角点「⚙ 后台」，或直接访问 `/#admin`。

| 页签 | 功能 |
|---|---|
| **数据概览** | 今日新增、总量、收藏、厂商覆盖、7 日趋势、今日高相关预览 |
| **大华功能** | 手动添加 / 批量粘贴导入大华平台功能清单（每行 `功能名\|描述\|分类`） |
| **竞品分析** | 自动生成潜在差距项（友商有/大华未覆盖）、厂商覆盖度、大华功能行业热度，支持导出 HTML |
| **采集推送** | 立即采集、推送今日 Top3、测试推送、定时时间/PushPlus Token 配置、AI 连接测试 |
| **质量门禁** | 查看被拦截的低质条目，可深抓复检或手动恢复入库 |
| **运行日志** | 采集/推送/AI/系统日志历史 |
| **访问统计** | PV/UV、来源、设备、IP 归属地明细 |

## 数据与采集机制

- **存储**：SQLite（本地 / CloudBase 云硬盘）/ PostgreSQL（设置 `DATABASE_URL` 自动切换），双兼容代码不用改
- **采集源**：厂商官网新闻页（主源）+ 搜狗网页 + 必应中国 + 百度资讯（辅源），遇验证码自动跳过并记录
- **去重**：按 URL + 标题全局去重，跨日期不重复
- **AI 提炼**：每批 10 条送智谱 GLM-4-Flash，要求每条输出 120~200 字三段式摘要 + 0~5 分 + 标签；< 100 字会自动补写一轮
- **相关度**：AI 评分优先，无 AI 时用关键词加权（数字孪生/视频融合/三维可视化 = 高权重）
- **标签**：技术 / 方案 / 政策 / 竞品 / 案例 / 展会（AI 输出，降级时关键词识别）
- **质量门禁**：基于文本/链接比例等指标识别低质页，拦截进 `quarantine` 表不直接丢
- **定时**：APScheduler 后台调度，时区 `Asia/Shanghai`，重启后按数据库配置自动恢复；多实例有 DB 锁防重跑
- **备份**：SQLite 周期性全量复制到 `BACKUP_DIR`（默认 600s 节流），容器重启自动尝试恢复

## 目录结构

```
intel-radar/
├── app.py              # Flask 主应用（路由/鉴权/竞品分析/调度）
├── collector.py        # 采集器（官网 + 搜索引擎 + 媒体提取 + 质量门禁）
├── ai.py               # AI 提炼（OpenAI 兼容协议，默认智谱）
├── pusher.py           # 微信 PushPlus 推送
├── daily_focus.py      # 公开页"每日关注"五维度聚合
├── database.py         # SQLite / PostgreSQL 双兼容数据层
├── config.py           # 全局配置（厂商/行业/关键词/阈值，全部可被环境变量覆盖）
├── templates/
│   └── index.html      # 唯一前端页面（PC + 手机自适应，原生 JS，无构建）
├── deploy/
│   └── tcb_deploy.py   # CloudBase OpenAPI 自动部署脚本（GitHub Actions 调用）
├── .github/workflows/deploy.yml   # main push 自动部署到 CloudBase
├── Dockerfile          # CloudBase 云托管镜像（python:3.11-slim + gunicorn）
├── requirements.txt
├── Procfile / render.yaml   # 历史 PaaS 配置（CloudBase 为主后可忽略）
├── run.bat             # Windows 本地一键启动
├── AGENTS.md           # 给开发者/AI Agent 的上手指南
├── HANDOFF.md          # 当前进度与部署现场快照
└── README.md
```

## 常见问题

- **采集很慢？** 单次约 30+ 组查询，每组 1-2 秒限频，整体 1-3 分钟正常；可后台「立即采集」看实时日志
- **某厂商没采到？** 可能是官网改版 404/反爬（改 `collector.py` 的 `OFFICIAL_CONFIG`），或搜索引擎对低频厂商返回少（在 `config.VENDORS` 补关键词）
- **微信推送收不到？** 后台「测试推送」先验证；再看 `PUSHPLUS_TOKEN` 是否过期、公众号是否取关
- **AI 不生效？** 后台「AI 设置 → 测试连接」；没配 Key 或调用失败会**静默降级**，不报错但日志里有 `ai` 模块 warn
- **后台登录不进去？** 确认环境变量 `ADMIN_PASSWORD` 已设置；改完环境变量需重新部署/重启进程
- **容器重启数据丢了？** CloudBase 必须挂云硬盘到 `/data` 并设 `DATA_DIR=/data`；再配 `BACKUP_DIR` 做对象存储兜底
- **手机访问不了？** 云部署后用域名访问；本地调试手机和电脑需同一 WiFi，访问 `http://电脑IP:5000`
