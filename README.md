# 📡 行业情报雷达（数字孪生 · 视频融合）

面向数字孪生/视频融合产品经理的行业情报平台：**每天自动采集厂商动态 → 微信推送 Top3 → 导入自有功能 → 自动生成竞品分析报告**。

## 功能总览

| 模块 | 说明 |
|---|---|
| **情报日报流** | 按日期查看每日采集的行业情报，支持厂商/行业/标签/相关度/关键词筛选，PC 与手机自适应 |
| **每日自动采集** | 后端调度器每天定时从**搜狗微信搜索**（免费、国内可用）按「厂商 × 行业」组合抓取公众号文章，自动打标签、评相关度 |
| **微信推送** | 每天早 8 点把相关度最高的 3 条推送到微信（**PushPlus 免费方案**） |
| **后台管理** | 密码登录（前台不展示入口），可导入大华功能清单、生成竞品分析报告、手动采集/推送、配置定时 |
| **竞品分析** | 自动比对「大华功能清单 vs 采集情报」，输出潜在差距项、厂商覆盖度、行业热点功能 |

## 已覆盖厂商（12 家，可扩展）

海康威视、智汇云舟、51WORLD、优锘科技、大华股份、华为、腾讯云、阿里云、超图软件、数字冰雹、商汤科技、百度智能云

## 已覆盖行业

智慧医院、智慧校园、建筑、景区、园区（全部限定数字孪生/视频融合/三维可视化主题）

---

## 快速开始（本地运行）

```bash
# 1. 安装依赖
pip install -r requirements.txt

# 2. 启动（Windows 可直接双击 run.bat）
python app.py

# 3. 访问
#    前台: http://127.0.0.1:5000
#    后台: http://127.0.0.1:5000/#admin   （默认密码 luban2026，务必修改）
```

---

## 云端部署

### 方案 A：Render.com（推荐，免费额度）

1. 注册 [render.com](https://render.com)（GitHub 登录）
2. 创建 **Web Service**，连接本项目的 GitHub 仓库
3. 配置：
   - **Build Command**: `pip install -r requirements.txt`
   - **Start Command**: `gunicorn app:app --bind 0.0.0.0:$PORT --timeout 120`
   - **Environment Variables**:
     - `ADMIN_PASSWORD` = 你的后台密码
     - `PUSHPLUS_TOKEN` = 你的 PushPlus token（也可后台填）
     - `SECRET_KEY` = 随机字符串
4. 部署完成后访问 `https://xxx.onrender.com`
   - ⚠️ Render 免费层在无访问时会休眠，唤醒后调度任务可能延后。如需稳定每日 8 点推送，见方案 C

### 方案 B：Railway / Fly.io / PythonAnywhere

流程类似：连接仓库 → 安装依赖 → 启动 gunicorn → 配置环境变量。
PythonAnywhere 免费层支持 Flask + 定时任务（Schedule 面板），适合国内访问。

### 方案 C：自有 VPS（最稳定，推荐长期使用）

```bash
# 服务器上执行（以 Ubuntu 为例）
apt install python3-pip
git clone <你的仓库> intel-radar && cd intel-radar
pip install -r requirements.txt

# 方式1：直接跑（简单）
ADMIN_PASSWORD=xxx PUSHPLUS_TOKEN=xxx nohup python app.py &

# 方式2：systemd 守护（推荐，自动重启）
cat > /etc/systemd/system/intel-radar.service <<'EOF'
[Unit]
Description=Intel Radar
After=network.target

[Service]
WorkingDirectory=/root/intel-radar
Environment=ADMIN_PASSWORD=xxx
Environment=PUSHPLUS_TOKEN=xxx
ExecStart=/usr/bin/python3 app.py
Restart=always

[Install]
WantedBy=multi-user.target
EOF

systemctl enable --now intel-radar
```

> 服务器在中国大陆：采集使用搜狗微信搜索，无需翻墙；服务器在海外：默认数据源不变，也兼容。

---

## 微信推送配置（PushPlus，完全免费）

1. 手机打开 **https://www.pushplus.plus/** ，微信扫码登录
2. 按引导**关注公众号「pushplus推送加」**
3. 复制页面上的 **token**
4. 打开本系统后台 → **采集推送** 页签 → 粘贴 token → 保存设置 → 点「测试推送」验证
5. 每天 8:00 你会收到「📡 行业情报雷达日报」推送

> 备选：Server酱（sct.ftqq.com，免费额度每日 5 条）——本项目按 PushPlus 实现，如改用请改 `pusher.py` 中 `send_push()`。

---

## 后台使用说明

进入方式：前台页面右上角点「⚙ 后台」，或直接访问 `/#admin`

| 页签 | 功能 |
|---|---|
| **数据概览** | 今日新增、情报总量、收藏数、厂商覆盖 + 今日高相关预览 |
| **大华功能** | 手动添加 / 批量粘贴导入大华平台功能清单（每行 `功能名\|描述\|分类`） |
| **竞品分析** | 自动生成：潜在差距项（友商有/大华未覆盖）、各厂商情报覆盖度、大华功能行业热度 |
| **采集推送** | 立即采集、推送今日 Top3、测试推送、定时时间设置、PushPlus Token 配置 |
| **运行日志** | 采集/推送历史记录 |

## 数据与采集机制

- 数据存储：SQLite（`data/radar.db`），无需额外数据库服务
- 采集源：搜狗微信搜索（厂商公众号文章）+ 搜狗网页搜索（备用），遇到验证码自动跳过该查询并记录日志
- 相关度评分：标题+摘要关键词加权（数字孪生/视频融合/三维可视化 = 高权重）
- 标签分类：技术 / 方案 / 政策 / 竞品 / 案例 / 展会（自动识别关键词）
- 定时任务：内置 APScheduler，重启服务后自动按设置恢复

## 目录结构

```
intel-radar/
├── app.py          # Flask 主应用（路由/鉴权/竞品分析/调度）
├── collector.py    # 采集器（搜狗微信+网页）
├── pusher.py       # 微信推送（PushPlus）
├── database.py     # SQLite 数据层
├── config.py       # 配置（厂商/行业/关键词/时间）
├── templates/
│   └── index.html  # 响应式前端（PC+手机）
├── requirements.txt
├── Procfile        # 云平台启动配置
└── run.bat         # Windows 本地启动
```

## 常见问题

- **采集很慢？** 每次采集约 30 组查询 × 2 秒限频 ≈ 1-2 分钟，正常现象；可在后台「立即采集」观察日志
- **某厂商没采到？** 搜狗微信对低频厂商返回较少，可在 `config.py` 的 `VENDORS` 里补充关键词
- **手机访问不了？** 云部署后需放行端口/使用域名；本地测试手机与电脑需同一 WiFi，访问 `http://电脑IP:5000`
- **后台密码如何改？** 部署时设置环境变量 `ADMIN_PASSWORD`（生产环境强烈建议）
