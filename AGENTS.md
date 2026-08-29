# AGENTS.md — 行业情报雷达

> 本文件是给 AI Agent / 新开发者的"上手指南"。目标：**读完它就能安全地修改代码、定位 bug、执行发布**，不用在 5900 行代码里盲找。
>
> 配合阅读：`README.md`（用户向功能介绍）、`HANDOFF.md`（当前进度快照）。

---

## 一、项目一句话定位

面向数字孪生 / 视频融合产品经理的**行业情报自动雷达**：每天定时从 12 家厂商官网 + 搜索引擎抓资讯 → AI（智谱 GLM-4-Flash）提炼摘要并打分过滤 → 卡片化展示 + 微信 PushPlus 推送 Top3 → 支持大华功能清单对比生成竞品分析报告。

- **仓库**：`angeldebaba/intel-radar`（注意是 `angeldebaba`，不是 `ngeldebaba`）
- **主分支**：`main`
- **生产环境**：腾讯云 CloudBase 云托管（上海 `ap-shanghai`，envId `angel-d2gws9dnv51db45e2`，服务名 `angel`）
- **访问域名**：CloudBase 控制台分配的 `*.tcloudbaseapp.com`（生产密码不进仓库，从环境变量读）

---

## 二、技术栈

| 层 | 选型 |
|---|---|
| 语言 / 运行时 | Python 3.11（Dockerfile 锁定 `python:3.11-slim`，本地 3.12 亦可） |
| Web 框架 | Flask 2.3+ |
| WSGI | Gunicorn（2 workers, 120s timeout） |
| 定时任务 | APScheduler（BackgroundScheduler, 时区 `Asia/Shanghai`） |
| 数据库 | SQLite（本地）/ PostgreSQL 10+（云端，`DATABASE_URL` 自动切换） |
| 前端 | 单文件 `templates/index.html`，原生 JS + 内联 CSS，**无构建步骤** |
| AI | OpenAI 兼容协议，默认智谱 `glm-4-flash`（免费） |
| 推送 | PushPlus（`pushplus.plus`，免费微信通道） |
| 部署 | CloudBase 云托管（Docker 监听 80）+ GitHub Actions 自动发布 |

**没有**：React/Vue、npm、TypeScript、前端打包器、Celery、Redis。任何引入这些依赖的改动都要先确认需求。

---

## 三、目录结构与关键文件

```
intel-radar/
├── app.py              # Flask 主应用（~1500 行）：路由 / 鉴权 / 调度 / 竞品分析
├── collector.py        # 采集器（~1890 行）：官网 + 搜索引擎 + RSS + 公众号品牌词抓取 / 媒体提取 / 质量门禁
├── database.py         # 数据层（~570 行）：SQLite & PG 双兼容、自动迁移、备份
├── ai.py               # AI 提炼（~180 行）：批量调用 + 降级 + 短摘要重试
├── daily_focus.py      # 后台"每日关注"五维度聚合快照（鉴权后可见）
├── hot_stats.py        # 后台"📊 情报看板"：30 天趋势 / 热词 / 厂商-行业-来源分布 / 相关度 / 头条
├── industry_overview.py # 公开页"🌐 行业观察"：行业全局视角的市场规模/技术迁移/应用/区域/竞争/风险/未来趋势（结构化研究快照，不依赖抓取数据）
├── pusher.py           # 微信 PushPlus 推送
├── config.py           # 全局配置：厂商/行业/关键词/阈值，全部可被环境变量覆盖
├── templates/
│   └── index.html      # 唯一前端页面（~1600 行，内嵌 JS/CSS）
├── deploy/
│   ├── tcb_deploy.py    # CloudBase OpenAPI 部署脚本（GitHub Actions 调用）
│   └── tcb_scale.py     # CloudBase 副本数运维脚本（--status 查询 / --fix-one 固定单实例）
├── .github/workflows/
│   ├── deploy.yml       # main 分支 push 即自动部署
│   └── ops-scale.yml    # 手动触发：Actions → "Ops - CloudBase Scale" 查询/固定副本数
├── Dockerfile          # CloudBase 镜像（python:3.11-slim + gunicorn）
├── render.yaml         # （历史遗留）Render 蓝图，已迁 CloudBase
├── Procfile            # 通用 PaaS 启动命令
├── run.bat             # Windows 本地一键启动（注意里面的 Python 绝对路径是个人机器的）
├── pushplus_cli.py / pushplus_setup.py   # PushPlus 配置辅助小脚本
└── requirements.txt
```

---

## 四、本地运行

```bash
# 1. 建虚拟环境（推荐 Python 3.11/3.12）
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
# APScheduler 若报 tzdata 缺失：pip install tzdata

# 2. 准备环境变量（最小集合）
export DATA_DIR=./data             # SQLite 存放目录
export ADMIN_PASSWORD=luban2026    # 后台密码，本地随便填
# 可选：export AI_API_KEY=...      # 智谱 key，没有也能跑（降级关键词打分）
# 可选：export PUSHPLUS_TOKEN=...  # 没配就不推送

# 3. 启动
python app.py
# 前台：http://127.0.0.1:5000
# 后台：http://127.0.0.1:5000/  → 右上角"⚙ 后台"，密码 = ADMIN_PASSWORD
```

Windows 用户可双击 `run.bat`，但首次使用要把里面硬编码的 Python 路径改成自己的 venv 路径。

---

## 五、生产部署（CloudBase）

### 触发方式

**push 到 `main` 分支** → GitHub Actions 自动跑 `deploy/tcb_deploy.py` → 调用腾讯云 TCBR OpenAPI 触发云托管镜像构建 → 轮询到成功。

`.github/workflows/deploy.yml` 关键配置：
- 服务名：`angel`
- 地域：`ap-shanghai`
- envId：`angel-d2gws9dnv51db45e2`
- 分支：`main`
- 并发：`concurrency: deploy-production`，不取消正在跑的任务，排队执行

### 必需的 GitHub Secrets

| Secret | 说明 |
|---|---|
| `TENCENT_SECRET_ID` | 子账号 SecretId（需 `QcloudTCBFullAccess` + `QcloudTCBRFullAccess`） |
| `TENCENT_SECRET_KEY` | 对应 SecretKey |

### CloudBase 服务端需要配置的环境变量

见 `config.py`，生产**必须**设置：

| 变量 | 说明 | 默认值 |
|---|---|---|
| `ADMIN_PASSWORD` | 后台登录密码（**生产必填**） | `luban2026` |
| `SECRET_KEY` | Flask session 密钥 | `intel-radar-secret-2026` |
| `AI_API_KEY` | 智谱 API Key | 空（降级关键词） |
| `PUSHPLUS_TOKEN` | PushPlus token | 空 |
| `DATA_DIR` | SQLite 目录 | `/tmp/radar_data` |
| `BACKUP_DIR` | 对象存储备份挂载点 | 空 |

其余可选项（阈值类）见 `config.py`，**一般无需修改**。

### 持久化

- CloudBase 云托管**容器重启会丢失本地盘**，必须挂云硬盘到 `/data`，并设置 `DATA_DIR=/data`
- 若再挂对象存储到某个路径（如 `/mnt/backup`），设置 `BACKUP_DIR` 指向它，`database.backup_db()` 会按 `BACKUP_INTERVAL`（默认 600s）节流备份
- **不要把 SQLite 直接放对象存储挂载目录**：FUSE 不支持文件锁，会报 disk I/O error

### 实例数

保持 **1 个实例**。代码里已有跨实例防重跑锁（`_job_lock_acquire`），但多实例仍可能让 Web 访问行为不一致，单实例最省心。

**检查/固定实例数**（推荐走 GitHub Actions，无需本地配密钥）：
1. GitHub 仓库 → Actions 标签页 → 左侧选 **"Ops - CloudBase Scale"** → Run workflow
   - `action=status`：只查询，不改动（看 MinNum/MaxNum/在线 Pod）
   - `action=fix-one`：直接固定为 1 个实例（MinNum=1, MaxNum=1, manualScale）
2. 或本地跑（需先 `export TENCENT_SECRET_ID/KEY`）：
   ```bash
   python3 deploy/tcb_scale.py --status
   python3 deploy/tcb_scale.py --fix-one
   ```
   脚本只改 Items 配置（副本数+运行模式），DeployInfo 用已在线镜像占位，**不会触发镜像重新构建**。
3. 控制台兜底路径：云托管 → 服务 `angel` → 服务设置 → 副本设置 → 最小 1 / 最大 1 / 手动调节。

---

## 六、核心模块速查

### `app.py`

- `app = Flask(__name__)`：全局应用，`database.init_db()` 在模块加载时即执行
- 后台路由以 `/api/admin/*` 开头，统一用 `@require_admin` 装饰器校验 session
- `_reschedule()`：根据数据库 `config` 表的时间重建 APScheduler 任务（采集 / 推送）
- `job_collect()`：每日采集，先抢 PG 行锁再执行，防多实例重复；结束后触发 `daily_focus.generate_today()`
- `job_push()`：每日推送 Top3
- 访问统计：`_track_enqueue` 入内存队列 → `_visit_flush_loop` 后台线程每 5s 批量落库 + IP 归属地富化（ip-api.com，免费但限频）

### `collector.py`

- `OFFICIAL_CONFIG`：**9 家**厂商官网新闻/方案页清单（VENDORS 有 12 家，部分厂商只走搜索引擎）
- `fetch_official_page` → `_parse_structured_list` / `_parse_generic_links`：通用列表页解析
- 搜索引擎：`fetch_sogou_web` / `fetch_bing` / `fetch_baidu_news`，每个引擎都有 `_engine_blocked` 节流（被反爬自动跳过）
- `fetch_rss_source` / `fetch_all_rss`：行业媒体 RSS/Atom 直采（feedparser），按 `config.RSS_SOURCES` 关键词过滤；feed 完整正文存 `description` 供评分/AI 使用。**RSS 源仅保留国内（36氪、雷锋网）**——2026-08-29 曾加 6 个海外英文源，实测外文内容相关度偏低、噪音大，已回退
- 注意请求头 `Accept-Encoding` 只声明 `gzip, deflate`（**不能加 `br`**）：requests 原生不解压 brotli，必应默认返回 br 会导致乱码、解析 0 条
- `collect_once()`：主编排，五阶段顺序执行——A. 官网 → B. 厂商×关键词搜索 → C. 行业专项搜索 → D. 行业媒体/公众号品牌词搜索 → E. RSS 直采；全局按 URL 去重
- `quality_verdict()`：内容质量门禁，不过的进 `quarantine` 表（不直接丢）
- `_enrich_article_media`：抓原文页提取 og:image / 视频 / 正文外链追溯
- AI 调用在 collector 中分批走 `ai.analyze_batch()`，低于 `AI_MIN_SCORE` 丢弃

### `database.py`

- 通过 `PG = bool(DATABASE_URL)` 在 SQLite / PostgreSQL 之间切换，占位符用 `PH = '%s' if PG else '?'`
- `init_db()` 建表 + 老库自动 `ALTER TABLE` 迁移（关键模式：SQLite 走 `PRAGMA table_info`，PG 走 `ADD COLUMN IF NOT EXISTS`）
- 改表结构时**必须**沿用这个模式，不能假设库是新建的
- `query / query_one / execute / insert` 是统一入口
- `backup_db()` 有节流，不要在热路径里调 `force=True`

### `ai.py`

- `SYSTEM_PROMPT` 强约束：每条 summary 必须 120~200 汉字、三段结构、严格 JSON
- 英文来源：AI 返回中文 summary + 中文 title（英文标题翻译、中文标题原样）；`analyze_batch()` 结果含 `title` 字段，collector 入库与 app.py reprocess 仅在**原标题为英文且译名含中文**时替换标题
- `analyze_batch()` 返回与输入等长的列表；失败返回 `None` 让调用方降级
- `_repair_short()`：对 <100 字的摘要做一轮补写重试
- 切换模型：改环境变量 `AI_API_BASE` / `AI_MODEL` 即可（例如 DeepSeek：`AI_API_BASE=https://api.deepseek.com/v1`、`AI_MODEL=deepseek-chat`）

### `daily_focus.py`

- 把当日情报按五维度（行业动态 / 产品 / 技术 / 市场 / 关注点）归并，生成快照存 `config` 表
- 每晚采集后由 `job_collect` 触发；后台也可手动刷新

### `hot_stats.py`

- 数字孪生行业热点统计看板的聚合层，每晚采集后由 `job_collect` 调用 `generate_today()` 写入快照
- `build_stats(days=30)`：实时聚合——总览 KPI、30 天趋势、厂商 / 行业 / 来源分布、相关度分布、标签云、领域热词（`DOMAIN_TERMS` 加权）、高相关头条
- `save_snapshot` / `load_snapshot` / `list_snapshot_dates`：快照走 `config` 表，key 形如 `hot_stats:YYYY-MM-DD`，值为 JSON
- `GET /api/hot-stats`：默认返回今日快照；若今日尚无快照则实时聚合并回填缓存，保证冷启动不空白
- 接口**需 `@require_admin`**（已收紧到后台可见，未登录返回 401）；`POST /api/hot-stats/refresh` 同步收紧
- 前端入口：顶部导航「📊 情报看板」，hash 路由 `#hot`，DOM 容器 `#view-hot` / `#hotBody`，渲染函数 `renderHot()` 在 `templates/index.html`

### `industry_overview.py`

- **行业全局观察**看板的数据源。主体是一份打包的结构化行业研究快照（市场规模、技术三次迁移、核心技术突破、应用场景分布、中国区域格局、竞争格局、核心挑战、未来趋势，不每日变动）；**动态部分由 AI 每晚立足【全球整个行业前沿】研判生成**（不限于本站采集）——顶部观察信号 `watch_signals` + 六维前沿研判 `frontier`（技术/产品/市场/政策/应用/趋势）+ 一句话风向 `frontier_headline`
- `DEFAULT_OVERVIEW`：仓库内置兜底快照，数值标注来源与年份（信通院、IDC、Gartner、MarketsandMarkets、Grand View Research 等）；不同口径数字并列展示，不盲目合并
- `get_snapshot(date=None)`：优先读数据库 `config` 表 key=`industry_overview:YYYY-MM-DD`；没有当日则取最近一份；再没有才返回内置默认
- `save_snapshot(data, date=None)`：后台或 AI 写入新版快照（全量覆盖；config 表只有 key/value 两列，**UPDATE 不要引用 updated_at**）
- `list_snapshot_dates(limit=30)`：列出有快照的日期
- `_recent_intel(days=7)`：取近 7 天情报标题/摘要作为"近期线索"（仅锚点，**可为空**）
- `build_frontier_briefing(clues)`：调 `ai._chat` 让 AI 以行业前沿认知为主产出 `{headline, signals[], frontier[六维]}`；线索仅作硬事实锚点，prompt 带防幻觉纪律（趋势/格局可展开，具体公司动作/融资/发布等硬事实只能引用线索、不得杜撰）；AI 未配置/失败返回 None
- `generate_today()`：供 `job_collect` **每晚**调用——以最近一份快照为底稿保留静态研究内容，用 AI 重写 `watch_signals`/`frontier`/`frontier_headline`（失败保留上一版），并**生成当天日期的新快照**；即使 0 采集也照常产出前沿观察
- 前端：`watch_signals`+`frontier_headline` 渲染为 `#industryBody` 顶部 `.watch-banner`（固定、不参与拖拽）；`frontier` 渲染为「🛰️ 行业前沿研判」`.frontier-grid` 六维卡片（key=`frontier`，可拖拽排序）
- `GET /api/industry-overview`（公开，`?date=YYYY-MM-DD` 可取历史）
- `GET /api/industry-overview/dates`
- `POST /api/admin/industry-overview`（**需 `@require_admin`**，body `{date?, data:{...}}`）
- 前端入口：顶部导航「🌐 行业观察」，hash 路由 `#industry`，DOM 容器 `#view-industry` / `#industryBody`，渲染函数 `renderIndustry()`

### 前端 `templates/index.html`

- 单文件、无构建，约 1600 行
- 页面通过 hash 路由：`#admin` 进入后台，前台公开页为默认
- 所有交互走 `fetch('/api/...')`，列表用无限滚动（不是传统分页器）
- 修改样式：直接改文件内 `<style>`；没有 Tailwind / SCSS

---

## 七、HTTP 接口清单

公开接口（无需登录）：

| 方法 | 路径 | 用途 |
|---|---|---|
| GET | `/` | 首页（渲染 index.html） |
| GET | `/article/<id>` | 存档原文查看页 |
| GET | `/api/article/<id>/text` | 存档原文纯文本（卡片「展开原文」懒加载；无存档返回 `{ok:false}` 前端回退 description） |
| GET | `/api/stats` | 首页统计数字（健康检查也用这个） |
| GET | `/api/stats/trend` | 近 7 日趋势 |
| GET | `/api/intelligence` | 情报列表（支持 vendor/industry/tag/keyword/排序） |
| GET | `/api/filters` | 筛选项元数据 |
| GET | `/api/dates` | 有数据的日期列表 |
| GET | `/api/industry-overview` | 行业全局观察（市场/技术/应用/区域/竞争/风险/趋势；来源为打包快照+DB 覆盖，不依赖抓取数据） |
| GET | `/api/industry-overview/dates` | 行业观察快照日期列表 |
| POST | `/api/favorite/<id>` | 收藏切换 |
| POST | `/api/track` | 访问埋点 |

后台接口（需要 `@require_admin` session）：
`/api/admin/login` · `/api/admin/logout` · `/api/admin/status` ·
`/api/daily-focus` · `/api/daily-focus/dates` ·
`/api/hot-stats` · `/api/hot-stats/refresh` (POST) · `/api/hot-stats/dates` ·
`/api/admin/analytics` · `/api/admin/visit-detail` ·
`/api/admin/dahua` (GET/POST) · `/api/admin/dahua/<id>` (DELETE) · `/api/admin/dahua/import-text` ·
`/api/admin/analysis` · `/api/admin/analysis/export` ·
`/api/admin/collect` · `/api/admin/collect-status` ·
`/api/admin/reprocess` · `/api/admin/reprocess-status` ·
`/api/admin/media-rescan` · `/api/admin/media-rescan-status` ·
`/api/admin/push-test` · `/api/admin/push-now` ·
`/api/admin/ai-status` · `/api/admin/diagnose` ·
`/api/admin/settings` (GET/POST) · `/api/admin/logs` ·
`/api/admin/quarantine` · `/api/admin/quarantine/<id>` · `/api/admin/quarantine/scan` · `/api/admin/quarantine/promote`

新增接口时遵守现有命名约定；后台接口**必须**加 `@require_admin`，否则就是越权漏洞。

---

## 八、数据模型

主要表（详见 `database.init_db()`）：

- `intelligence` — 情报主表（title/url/summary/image/relevance/tags/media/published/...）
- `article_archive` — 原文全文存档（HTML + plain_text，按 intel_id 关联）
- `dahua_features` — 大华平台功能清单
- `quarantine` — 质量门禁隔离区
- `config` — KV 配置（定时时间、PushPlus token、锁、config_version、`daily_focus:YYYY-MM-DD`、`hot_stats:YYYY-MM-DD` 等快照）
- `collect_log` — 采集/推送日志（后台"运行日志"）
- `visit_log` / `visit_session` / `visit_daily` — 访问统计三层（明细 / 会话 / 日聚合）

字段新增流程：
1. 在 `init_db()` 的 `CREATE TABLE` 里加字段（新库生效）
2. 紧接着在 SQLite/PG 兼容块里加 `ALTER TABLE ... ADD COLUMN`（老库迁移）
3. 不要删字段、不要改字段类型，避免线上库损坏

---

## 九、代码风格与约定

- **Python**：纯标准库 + 已列依赖，不要随便引入新第三方包；`requirements.txt` 已尽量精简
- **缩进 4 空格**，文件头保留 `# -*- coding: utf-8 -*-`
- **中文注释 / commit message**，commit 前缀遵循 Conventional Commits（`feat:` / `fix:` / `chore:`），看 `git log` 即可
- **禁止硬编码密钥/token**，一律走 `config.py` 读环境变量或数据库 `config` 表
- **print 输出会进容器日志**，关键节点保留 `[时间] 事件` 格式；敏感字段（token、密码、完整请求体）不要打
- **异常处理**：后台任务（采集/推送/访问落库）必须兜底，单条失败不能让整个调度崩
- **前端无构建**：JS 直接写在 `<script>` 里，不要引入 npm/打包链

---

## 十、常见排障路径

| 症状 | 优先检查 |
|---|---|
| 云端定时采集不跑 | 1) CloudBase 服务日志是否有报错 2) `config` 表 `coll_time` 值 3) 是否多实例（防重跑锁是否被占） |
| 采集新增 0 条 | 1) 后台"采集状态"日志 2) 引擎是否被反爬（`_engine_blocked` 会记日志）3) 官网 URL 是否 404（看 `OFFICIAL_CONFIG`） |
| 微信推送收不到 | 1) 后台"测试推送"按钮 2) `PUSHPLUS_TOKEN` 是否过期 3) PushPlus 官网是否要求重新关注公众号 |
| AI 不生效 | 1) 后台"AI 设置 → 测试连接" 2) `AI_API_KEY` / `AI_API_BASE` 3) 智谱免费额度是否用尽 4) 失败会自动降级，不报错不代表在用 AI |
| 页面 500 | gunicorn 错误日志 → 定位到具体 Python traceback；最常见是 SQL 兼容问题（SQLite vs PG 的占位符/方言） |
| 容器重启数据丢了 | 检查云硬盘是否挂载、`DATA_DIR` 是否指向挂载路径、`BACKUP_DIR` 是否有备份文件 |
| 图片/视频不显示 | `media` 字段 JSON 格式是否正确；原始链接是否失效；后台"重抓媒体"可重试 |
| 后台登录不进去 | 确认 CloudBase 环境变量 `ADMIN_PASSWORD` 已设置；改完环境变量需要重新部署才生效 |

**看生产日志**：CloudBase 控制台 → 云托管 → 服务 `angel` → 版本列表 → 日志。
本仓库没有写日志文件，全部 stdout，由 CloudBase 采集。

---

## 十一、已知技术债 / 待办

来源：`HANDOFF.md` 与代码扫描，持续维护中：

- [ ] 部分官网采集源可能 404/403（超图 `supermap.com.cn`、数字冰雹 `digihail.com`、51WORLD 首页），需定期回归 `OFFICIAL_CONFIG`
- [ ] `render.yaml` / `Procfile` 是 Render 时代遗留，CloudBase 为主后可考虑清理
- [ ] `run.bat` 里硬编码了个人 Python 路径，换机必改（或改用 `python app.py`）
- [ ] `config.py` 中 `ADMIN_PASSWORD` 默认值 `luban2026` 仅作本地兜底，生产必须覆盖
- [ ] README 里 CloudBase 与 Render 两套部署说明并列，容易让新用户困惑（已在本次更新中收敛）

---

## 十二、给 AI Agent 的操作守则

1. **改动前先读 `HANDOFF.md`**：那里记录了当前状态、最近变更和未完成事项
2. **改完跑本地**：`python app.py` 启动无报错 + 访问 `http://127.0.0.1:5000/api/stats` 返回 200
3. **数据库改动**：必走 `init_db()` 兼容迁移，不要写独立迁移脚本除非真的必要
4. **不要直接在 main 分支提交**：从 main 切 feature 分支，commit 后发 PR；紧急 hotfix 可直推 main（会自动部署）
5. **任何密钥都不要提交**：本地 `.env` 已被 `.gitignore` 覆盖（如未覆盖请加上）
6. **前端改动无构建步骤**：刷新浏览器即可看到效果，CloudBase 部署完成后强刷（Ctrl/Cmd+Shift+R）
7. **不要杀死 / 改动 9000 端口相关的任何进程**（沙箱系统保留）
8. **本项目是 Python 项目，不是 Node 项目**：不要跑 `pnpm install` / `npm`；依赖管理用 `pip`
9. **不要使用 `coze init` 重新初始化这个目录**——它已经是一个成熟的 Python/Flask 项目
10. **不要凭空猜测 API 或函数签名**：改代码前先 `grep` 确认现有实现，所有改动必须能在当前代码库里找到对应上下文
