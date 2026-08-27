# 🔄 HANDOFF — 项目现场快照（2026-08-27）

> 换电脑 / 换环境恢复现场用。配合 README.md（用户向）和 AGENTS.md（开发者向）阅读：
> README 讲怎么跑、AGENTS 讲怎么改、本文件讲"现在进行到哪了"。

## 一、当前状态（一句话）

**全链路已上线 CloudBase 云托管生产环境并稳定运行**：
定时采集 → AI 提炼/过滤 → 卡片展示 → 每日关注聚合 → 微信推送，全链路自动。
最近一次迭代重心：**公开页仅保留情报流 + 「🌐 行业观察」全局看板**，**「📋 每日关注」「📊 情报看板」收紧到后台可见**（对应 API 加 `@require_admin`，未登录 401）；**新增「🌐 行业观察」全局看板（行业研究视角）** + **采集源扩充（RSS / 行业媒体 / 公众号品牌词）** + **内容质量门禁（隔离区）** + **AI 摘要 120~200 字硬约束**。

## 二、生产环境关键事实（不要再按 Render 找）

- **平台**：腾讯云 CloudBase 云托管（不再是 Render，Render 已停用）
- **地域 / envId**：`ap-shanghai` / `angel-d2gws9dnv51db45e2`
- **服务名**：`angel`
- **部署方式**：push 到 `main` → GitHub Actions（`.github/workflows/deploy.yml`）→ `deploy/tcb_deploy.py` 调 TCBR OpenAPI 触发镜像构建
- **镜像**：`python:3.11-slim` + `gunicorn app:app -b 0.0.0.0:80 --workers 2 --timeout 120`
- **数据库**：SQLite（`DATA_DIR=/data/radar.db`，云硬盘持久化到 `/data`）
  - 历史上用过 Render 的免费 PostgreSQL，现已迁回 SQLite + 云硬盘 + `BACKUP_DIR` 节流备份
  - 代码仍保留 PG 兼容（`DATABASE_URL` 环境变量自动切换），未来要切 PG 也不用改代码
- **实例数**：保持 1 个（多实例时靠 `_job_lock_acquire` 防重复采集/推送）
- **必需的 GitHub Secrets**：`TENCENT_SECRET_ID`、`TENCENT_SECRET_KEY`（子账号，授予 `QcloudTCBFullAccess` + `QcloudTCBRFullAccess`）
- **看日志**：CloudBase 控制台 → 云托管 → 服务 `angel` → 版本列表 → 日志（全部 stdout，无本地日志文件）

## 三、最近完成的工作（按时间倒序，节选）

1. **公开页「🌐 行业观察」全局看板** —— 新增 `industry_overview.py`，**行业研究视角**（与本站抓取数据解耦），含全球/中国市场规模、技术三次迁移、2026 核心技术突破、应用场景分布、中国区域格局、竞争格局（全球头部 + 中国第一梯队）、核心挑战与风险、未来 6 大趋势；数据来自信通院/IDC/Gartner/MarketsandMarkets 等公开报告并标注来源；打包在 `DEFAULT_OVERVIEW` 兜底，后台可通过 `POST /api/admin/industry-overview` 写入新版本覆盖；每晚采集任务结束后自动把内置快照落库一次（`config` 表 key=`industry_overview:YYYY-MM-DD`）；前端 hash 路由 `#industry`
2. **「📋 每日关注」「📊 情报看板」收紧到后台** —— 公开页只保留情报流 + 「🌐 行业观察」；两个内部看板移到后台侧边栏 Tab，对应 `/api/daily-focus*`、`/api/hot-stats*` 全部加 `@require_admin`，未登录返回 401；历史 hash `#focus` / `#hot` 会自动检测登录态，已登录则跳到后台对应 Tab，未登录跳登录页
3. **采集数据源扩充** —— 新增 RSS/Atom 直采（`feedparser`，`config.RSS_SOURCES`：中新网科技/人民网科技/新华网科技，按关键词过滤）；新增行业媒体与公众号品牌词搜索阶段（`INDUSTRY_MEDIA_QUERIES`，20 条覆盖泰伯网/36氪/智东西/机器之心/微信公众号等）；`collect_once()` 由三阶段扩展为五阶段（官网→厂商搜索→行业搜索→媒体/公众号搜索→RSS）
4. **「每日关注」五维度聚合（后台）** —— 行业动态/产品/技术/市场/关注点当日聚合，`daily_focus.py` 生成快照存 `config` 表，采集完成后自动刷新
5. **内容质量门禁** —— `quality_verdict()` 拦截"仅含链接无实质文本"的低质条目，进 `quarantine` 表备查，后台可审查/深抓复检/恢复入库
6. **全文存档 + 原文查看** —— `article_archive` 表保存净化 HTML 与纯文本，`/article/<id>` 可本地回看（防原文链接过期/反爬）
7. **AI 摘要长度硬约束** —— prompt 强制 120~200 字三段结构；<100 字自动 `_repair_short()` 补写重试
5. **相关度阈值提质** —— `AI_MIN_SCORE` 与 `MIN_RELEVANCE` 调到 3；`RELEVANCE_HIGH` 加入"视频孪生/三维/可视化"
6. **媒体提取增强** —— 正文外链视频追溯、og:image 抓取、推荐位/广告/头像/二维码过滤、头图按实际宽高比自适应
7. **访问统计模块** —— 埋点 + 会话时长 + 来源追踪 + IP 归属地批量富化 + 后台明细列表（90 天滚动清理）
8. **跨实例防重跑锁** —— 基于 DB `config` 表的令牌锁，CloudBase 滚动部署时新老实例不会重复跑任务
9. **备份节流** —— `BACKUP_INTERVAL=600s`，避免逐条写库就全量备份拖垮采集
10. **修复致命 bug**：`collector.py` 引用不存在的 `config.OFFICIAL_CONFIG`（曾导致云端定时采集每晚崩溃）
11. **后台未鉴权漏洞修复**：「⚙ 后台」入口先跳登录页

## 四、环境变量清单（名称进文档，值不进仓库！）

生产环境在 CloudBase 服务配置里设置；本地用 `export` 或 `.env`（已 gitignore）。

| 变量 | 说明 | 生产必须 |
|---|---|---|
| `ADMIN_PASSWORD` | 后台登录密码（`config.py` 默认 `luban2026` 仅本地兜底） | ✅ |
| `SECRET_KEY` | Flask session 密钥 | ✅ |
| `AI_API_KEY` | 智谱 Key（open.bigmodel.cn，`glm-4-flash` 免费） | 推荐 |
| `AI_API_BASE` | 默认 `https://open.bigmodel.cn/api/paas/v4`，可换 DeepSeek 等 | 可选 |
| `AI_MODEL` | 默认 `glm-4-flash` | 可选 |
| `AI_MIN_SCORE` | AI 评分低于此值丢弃，默认 3 | 可选 |
| `PUSHPLUS_TOKEN` | PushPlus 微信推送 token | 推荐 |
| `DATA_DIR` | SQLite 目录，生产 `/data` | ✅ |
| `BACKUP_DIR` | 对象存储备份挂载点（容器重启可恢复） | 推荐 |
| `BACKUP_INTERVAL` | 备份节流秒数，默认 600 | 可选 |
| `COLLECT_TIME` | 每日采集时间，默认 `23:00` | 可选 |
| `PUSH_TIME` | 每日推送时间，默认 `08:00` | 可选 |
| `DAILY_FOCUS_TIME` | 每日关注快照时间，默认 `23:30` | 可选 |
| `RSS_MAX_PER_SOURCE` | 每个 RSS 源最多保留条数，默认 `10` | 可选 |
| `HOT_STATS_TREND_DAYS` | 热点统计趋势窗口天数，默认 `30` | 可选 |
| `FRESH_DAYS` | 文章发布时间超过 N 天不入库，默认 30 | 可选 |
| `INTEL_RETENTION_DAYS` | 情报保留天数，默认 90 | 可选 |
| `VISIT_RETENTION_DAYS` | 访问明细保留天数，默认 90 | 可选 |
| `QUARANTINE_ENABLED` | 质量门禁开关，默认 `1` | 可选 |
| `DATABASE_URL` | 若设置则切 PostgreSQL（CloudBase 现状不设置，走 SQLite） | 不需要 |

改 CloudBase 环境变量后**必须重新部署版本**才会生效（环境变量在容器启动时注入）。

## 五、新电脑恢复现场（10 分钟）

```bash
# 1. 拉代码
git clone https://github.com/angeldebaba/intel-radar.git
cd intel-radar
# （无 git 时直接下 zip：https://github.com/angeldebaba/intel-radar/archive/refs/heads/main.zip）

# 2. 建虚拟环境 + 装依赖（Python 3.11/3.12 均可）
python3 -m venv .venv
source .venv/bin/activate               # Windows: .venv\Scripts\activate
pip install -r requirements.txt
# APScheduler 若报 tzdata 缺失：pip install tzdata

# 3. 启动（本地测试，没有 AI Key 也能跑，会降级到关键词评分）
DATA_DIR=./data AI_API_KEY=你的key PUSHPLUS_TOKEN=你的token python app.py
# 前台 http://127.0.0.1:5000
# 后台 http://127.0.0.1:5000/        → 右上角"⚙ 后台"，密码 = ADMIN_PASSWORD

# 4. 验证 AI 通道：登录后台 → AI 设置 → 测试连接，应返回"模型响应: 正常"
#    Windows 用户也可直接双击 run.bat（首次需把里面硬编码的 Python 路径改成自己的 venv 路径）
```

本机不可迁移的内容（换机即失效，需要重建）：

- `data/radar.db`：本地测试数据库（生产数据在 CloudBase 云硬盘上，独立）
- `.venv/`：虚拟环境，重装即可
- 环境变量的值：去 CloudBase 控制台 / 智谱控制台 / PushPlus 官网查看

## 六、发布流程

1. 在本地切 feature 分支：`git checkout -b feature/xxx`
2. 改动 + 本地跑通 `python app.py`（至少访问 `/api/stats` 返回 200）
3. commit 用 Conventional Commits 前缀：`feat:` / `fix:` / `chore:` / `docs:`
4. 推到 GitHub 开 PR，合并到 `main` 后 GitHub Actions 自动部署
5. 紧急 hotfix 可直推 `main`（Actions 会自动排队执行，并发组 `deploy-production` 不取消在跑任务）
6. 看部署进度：GitHub 仓库 → Actions 页；部署成功后 CloudBase 控制台能看到新版本
7. 部署后强刷浏览器（Ctrl/Cmd+Shift+R），前端单文件无构建但有浏览器缓存

## 七、待办 / 已知问题

- [ ] 官网采集源部分可能 404/403：超图 `supermap.com.cn`、数字冰雹 `digihail.com`、51WORLD 首页，需要定期回归 `OFFICIAL_CONFIG`
- [ ] `render.yaml` / `Procfile` 是 Render 时代遗留，CloudBase 为主后可以考虑删除或挪到 `legacy/`
- [ ] `run.bat` 里硬编码了个人 Python 绝对路径（`C:\Users\李悦锋\.workbuddy\...`），换机必改；建议改成读 `python` from PATH
- [ ] 定期检查智谱 `glm-4-flash` 免费额度政策是否变化
- [ ] `ADMIN_PASSWORD` 默认值 `luban2026` 在 `config.py` 中是兜底，生产务必通过环境变量覆盖（CloudBase 已配置）

## 八、协作约定

- 数据库 schema 变更走 `init_db()` 内的 `PRAGMA table_info` / `ADD COLUMN IF NOT EXISTS` 兼容迁移，不写独立迁移脚本
- 新增后台接口**必须**加 `@require_admin`
- 新增第三方依赖先想清楚是否真的需要，项目刻意保持依赖精简（纯标准库 + 8 个直接依赖）
- 密钥/token 一律走环境变量或数据库 `config` 表，不进代码、不进 git、不进日志
- 本机若没有 git，可临时用 GitHub Contents API（PUT 单文件 + PAT），脚本即用即删，token 不落盘
