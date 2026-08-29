# -*- coding: utf-8 -*-
"""行业情报雷达 - 全局配置"""
import os

# ===== 基础 =====
SECRET_KEY = os.environ.get('SECRET_KEY', 'intel-radar-secret-2026')
DEBUG = os.environ.get('DEBUG', '1') == '1'
HOST = os.environ.get('HOST', '0.0.0.0')
PORT = int(os.environ.get('PORT', 5000))

# 数据库路径：SQLite 必须放在容器本地盘，不能放在对象存储挂载目录上
# （对象存储 FUSE 挂载不支持 SQLite 文件锁，会报 disk I/O error）
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.environ.get('DATA_DIR', '/tmp/radar_data')
DB_PATH = os.path.join(DATA_DIR, 'radar.db')

# 备份目录：对象存储挂载路径，用于容器重启后恢复数据
# CloudBase 对象存储通常挂载到 /mnt/ 或 /data/，按实际挂载填写
BACKUP_DIR = os.environ.get('BACKUP_DIR', '')

# ===== 后台管理 =====
# 后台密码（部署时务必通过环境变量 ADMIN_PASSWORD 覆盖！）
ADMIN_PASSWORD = os.environ.get('ADMIN_PASSWORD', 'luban2026')

# ===== 访问统计 =====
# 原始访问记录保留天数（超龄自动清理；visit_daily 日聚合表长期保留，体积小）
VISIT_RETENTION_DAYS = int(os.environ.get('VISIT_RETENTION_DAYS', '90'))

# ===== AI 提炼（OpenAI 兼容接口，默认智谱 GLM-4-Flash 免费） =====
# 默认智谱：https://open.bigmodel.cn 注册后创建 API Key（glm-4-flash 免费）
# 换 DeepSeek：AI_API_BASE=https://api.deepseek.com/v1 且 AI_MODEL=deepseek-chat
# 留空 Key 则采集降级为原关键词逻辑
AI_API_KEY = os.environ.get('AI_API_KEY', '')
AI_API_BASE = os.environ.get('AI_API_BASE', 'https://open.bigmodel.cn/api/paas/v4')
AI_MODEL = os.environ.get('AI_MODEL', 'glm-4-flash')
AI_MIN_SCORE = int(os.environ.get('AI_MIN_SCORE', '3'))  # AI 评分低于此值丢弃（2026-08-23: 2→3 提质）
AI_BATCH_SIZE = int(os.environ.get('AI_BATCH_SIZE', '10'))  # 每次请求合并分析的条数
# 相关度入库下限：关键词评分路径低于此值直接丢弃（与 AI_MIN_SCORE 对齐）
MIN_RELEVANCE = int(os.environ.get('MIN_RELEVANCE', '3'))

# ===== 采集配置 =====
# 采集与推送时间（24小时制 HH:MM，服务器时区）
COLLECT_TIME = os.environ.get('COLLECT_TIME', '23:00')    # 每晚采集，次日白天可见
PUSH_TIME = os.environ.get('PUSH_TIME', '08:00')          # 每日早上微信推送
MAX_PER_QUERY = int(os.environ.get('MAX_PER_QUERY', 6))   # 每个查询取前N条
# 手动采集冷却期（小时）：冷却期内重复采集因全局去重不会新增，且易触发搜索引擎风控
MANUAL_COOLDOWN_HOURS = float(os.environ.get('MANUAL_COOLDOWN_HOURS', '4'))

# 时效控制：文章发布时间超过N天不入库（官网+搜索引擎结果统一按此过滤）
FRESH_DAYS = int(os.environ.get('FRESH_DAYS', '30'))

# ===== 全文存档（防链接过期/反爬导致原文不可回看） =====
# 单次采集最多抓多少篇文章页做全文存档（含媒体提取，每篇 1-2 次 HTTP 请求）
ARCHIVE_FETCH_LIMIT = int(os.environ.get('ARCHIVE_FETCH_LIMIT', '30'))
# 单篇存档净化 HTML / 纯文本的长度上限（字符）
ARCHIVE_HTML_LIMIT = int(os.environ.get('ARCHIVE_HTML_LIMIT', '300000'))
ARCHIVE_TEXT_LIMIT = int(os.environ.get('ARCHIVE_TEXT_LIMIT', '100000'))

# ===== 内容质量门禁（2026-08-26） =====
# 拦截"仅含大量链接、无实质文本"的低质条目（首页/下载站/目录页/导航堆砌），
# 拦下的原始数据进 quarantine 隔离区备查，后台可审查/深抓复检/恢复入库
QUARANTINE_ENABLED = os.environ.get('QUARANTINE_ENABLED', '1') == '1'

# ===== 数据保留期 =====
# 情报及关联存档默认保留天数（后台可改，存 config 表 intel_retention_days；每晚采集后自动清理）
INTEL_RETENTION_DAYS = int(os.environ.get('INTEL_RETENTION_DAYS', '90'))

# ===== 备份节流 =====
# SQLite 全库备份到对象存储的最小间隔秒数（此前逐条写库都全量备份，库变大后会拖垮采集）
BACKUP_INTERVAL = int(os.environ.get('BACKUP_INTERVAL', '600'))

# ===== 每日关注（公开页聚合栏目） =====
# 按五大维度（行业动态/产品/技术/市场/关注点）归并展示当日情报
# 快照在每晚采集完成后自动生成，也可后台手动刷新
DAILY_FOCUS_TIME = os.environ.get('DAILY_FOCUS_TIME', '23:30')  # 快照生成时间（晚于采集）
DAILY_FOCUS_DAYS = int(os.environ.get('DAILY_FOCUS_DAYS', '1'))  # 窗口：展示近N天情报（默认当日）
DAILY_FOCUS_PER_DIM = int(os.environ.get('DAILY_FOCUS_PER_DIM', '12'))  # 每维度最多展示条数

# ===== 媒体抓取（原文图片/视频嵌入卡片） =====
# 每次采集最多抓多少篇文章页提取图片/视频（每篇多一次 HTTP 请求，控制总耗时）
MEDIA_ENRICH_LIMIT = int(os.environ.get('MEDIA_ENRICH_LIMIT', '10'))
# 单条情报最多保留图片数 / 视频数
MEDIA_MAX_IMAGES = int(os.environ.get('MEDIA_MAX_IMAGES', '4'))
MEDIA_MAX_VIDEOS = int(os.environ.get('MEDIA_MAX_VIDEOS', '2'))
# 媒体链接有效性校验超时（秒）
MEDIA_CHECK_TIMEOUT = int(os.environ.get('MEDIA_CHECK_TIMEOUT', '5'))
# 正文外链追溯：文章/描述提到的外部官网，最多追几个链接提取媒体（每个多 1-2 次请求）
MEDIA_FOLLOW_LINKS = int(os.environ.get('MEDIA_FOLLOW_LINKS', '2'))


# 厂商列表：name=中文名, key=搜索关键词（多个组合）
VENDORS = [
    {'name': '海康威视', 'keywords': ['海康威视 数字孪生', '海康威视 视频融合', '海康威视 三维可视化']},
    {'name': '智汇云舟', 'keywords': ['智汇云舟 数字孪生', '智汇云舟 视频融合', '云舟 数字孪生']},
    {'name': '51WORLD', 'keywords': ['51WORLD 数字孪生', '51world 三维可视化']},
    {'name': '优锘科技', 'keywords': ['优锘科技 数字孪生', 'UINO 数字孪生']},
    {'name': '大华股份', 'keywords': ['大华股份 数字孪生', '大华 视频融合']},
    {'name': '华为', 'keywords': ['华为 数字孪生 园区', '华为 数字孪生 建筑']},
    {'name': '腾讯云', 'keywords': ['腾讯云 数字孪生', '腾讯微瓴 数字孪生']},
    {'name': '阿里云', 'keywords': ['阿里云 数字孪生']},
    {'name': '超图软件', 'keywords': ['超图 数字孪生', 'SuperMap 三维GIS']},
    {'name': '数字冰雹', 'keywords': ['数字冰雹 数字孪生', '数字冰雹 可视化']},
    {'name': '商汤科技', 'keywords': ['商汤 数字孪生', '商汤 三维重建']},
    {'name': '百度智能云', 'keywords': ['百度智能云 数字孪生', '百度 数字孪生']},
    {'name': '易知微', 'keywords': ['易知微 数字孪生', 'EasyV 数字孪生', '易知微 可视化']},
    {'name': '飞渡科技', 'keywords': ['飞渡科技 数字孪生', '飞渡 CIM', '飞渡 三维']},
    {'name': '泰瑞数创', 'keywords': ['泰瑞数创 数字孪生', '泰瑞数创 SmartTwins', '泰瑞数创 三维']},
    {'name': '光辉城市', 'keywords': ['光辉城市 数字孪生', '光辉城市 DIVA', 'Mars 数字孪生']},
]

# 行业主题词（限定数字孪生/视频融合相关）
INDUSTRIES = ['智慧医院', '智慧校园', '建筑', '景区', '园区',
              '城市', '工厂', '交通', '能源', '水利']

# 行业专项查询（不限定厂商，覆盖更多场景与热词）
INDUSTRY_QUERIES = [
    '数字孪生 智慧医院',
    '数字孪生 智慧校园',
    '数字孪生 建筑',
    '数字孪生 景区',
    '数字孪生 园区',
    '视频融合 智慧医院',
    '视频融合 智慧校园',
    '视频融合 园区',
    '视频融合 建筑',
    '三维可视化 智慧园区',
    # 2026-08 扩充：补充行业热点/公众号/政策/技术趋势查询
    '数字孪生 智慧城市',
    '数字孪生 智慧工厂',
    '数字孪生 智慧交通',
    '数字孪生 智慧能源',
    '数字孪生 水利',
    '数字孪生 政策',
    '数字孪生 白皮书',
    '数字孪生 标准',
    '数字孪生 中标',
    '数字孪生 落地案例',
    'CIM 城市信息模型',
    'BIM GIS 融合',
    '实景三维中国',
    '低空经济 数字孪生',
    '视频孪生 智慧园区',
    'AI大模型 数字孪生',
    '数字孪生 融资',
]

# 行业媒体 / 公众号品牌查询（通过搜索引擎间接覆盖微信公众号与行业媒体内容，
# 不直接抓 mp.weixin.qq.com——腾讯系反爬严格，需登录/验证码）
INDUSTRY_MEDIA_QUERIES = [
    '数字孪生 微信公众号',
    '数字孪生 行业报告',
    '数字孪生 泰伯网',
    '数字孪生 36氪',
    '数字孪生 雷锋网',
    '数字孪生 智东西',
    '数字孪生 物联网智库',
    '数字孪生 亿欧',
    '数字孪生 极客公园',
    '数字孪生 新华网',
    '数字孪生 人民网',
    '数字孪生 中国电子报',
    '数字孪生 中国测绘',
    '数字孪生 智慧城市网',
    '数字孪生 千家网',
    '视频融合 媒体报道',
    '三维可视化 公众号',
    '孪生视界',
    '智能交通 数字孪生',
    '智慧能源 数字孪生',
]

# RSS/Atom 直采源（行业媒体稳定 feed；单源失败不影响其他）
# name: 来源名；url: feed 地址；vendor: 归类厂商（媒体源留空）；keywords: 命中其一才保留
#
# 维护记录：
#  - 2026-08-28 修正人民网/新华网 feed（tech.xml 404、时政频道错配）
#  - 2026-08-29 实测发现：中新网 scroll-news 为综合滚动频道（科技命中≈0）、
#    人民网 scitech feed 停更（最新 2025-04）、新华网 tech feed 冻结在 2022。
#    三个官媒 feed 均无法产出新鲜情报，故替换为持续更新的科技/行业媒体。
#  - 2026-08-29 曾新增 6 个海外垂直源（IoT/NVIDIA/Unity/Synced 等英文源），
#    实测外文内容相关度普遍偏低、噪音大，按需求回退为【仅国内源】。
# 校验方法：python3 -c "import feedparser; ..." 确认 status=200 且最新条目日期为近期。
RSS_SOURCES = [
    # ===== 国内科技 / 行业媒体（每日更新，中文关键词；仅保留 RSS 带全文摘要的源）=====
    {'name': '36氪', 'url': 'https://www.36kr.com/feed',
     'vendor': '', 'keywords': ['数字孪生', '智慧城市', '智慧园区', '物联网', '三维可视化',
                                 '视频融合', '数字经济', '人工智能', '低空经济', '车路云',
                                 '机器人', '具身智能', '自动驾驶']},
    {'name': '雷锋网', 'url': 'https://www.leiphone.com/feed',
     'vendor': '', 'keywords': ['数字孪生', '视频融合', '智慧城市', '物联网',
                                 '三维', '人工智能', '智能驾驶', '机器人', '具身智能',
                                 '自动驾驶', '边缘计算', '视觉']},
]

# RSS 抓取上限：每个源最多保留的条目数（关键词过滤后通常远少于此）
RSS_MAX_PER_SOURCE = 10

# 相关度评分关键词（2026-08-23: 新增 视频孪生/三维/可视化 入 HIGH，与 MIN_RELEVANCE=3 配合，
# 核心词单独命中即达入库线；三维/可视化 同时保留在 MEDIUM 形成叠加计分。
# 2026-08-29: 采集源回退为仅国内中文源后，移除为海外 RSS 添加的宽泛英文词
# （iot/sensor/3d/robotics 等易误命中英文泛内容）；仅保留少量高精准英文专有词兜底。）
RELEVANCE_HIGH = ['数字孪生', '视频融合', '三维可视化', '三维引擎', '数字底座', '孪生底座', '时空底座',
                  '视频孪生', '三维', '可视化',
                  'digital twin', 'digital twins', 'omniverse']
# MEDIUM 里的词与 HIGH 叠加计分：中文"三维/可视化"命中后可把单个 HIGH 词补到 3 分（达入库线）
RELEVANCE_MEDIUM = ['智慧医院', '智慧校园', '智慧园区', '可视化', '三维', 'BIM', 'GIS', 'CIM', '孪生', '元宇宙',
                    '智慧城市', '数字城市', '车路云']
RELEVANCE_LOW = ['安防', '监控', '摄像头', '物联网', 'AI', '人工智能', '大模型', '云平台']

# 标签分类
TAGS = ['技术', '方案', '政策', '竞品', '案例', '展会']

# ===== 微信推送（PushPlus，免费） =====
# 在 https://www.pushplus.plus/ 关注公众号后获取 token，填到后台"推送设置"或环境变量
PUSHPLUS_TOKEN = os.environ.get('PUSHPLUS_TOKEN', '')
PUSHPLUS_URL = 'https://www.pushplus.plus/send'

# ===== 微信推送模板 =====
PUSH_TOP_N = 3  # 每天推送条数

# 兼容旧环境的别名
USER_VENDORS = VENDORS
