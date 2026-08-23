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
]

# 行业主题词（限定数字孪生/视频融合相关）
INDUSTRIES = ['智慧医院', '智慧校园', '建筑', '景区', '园区']

# 行业专项查询（不限定厂商）
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
]

# 相关度评分关键词（2026-08-23: 新增 视频孪生/三维/可视化 入 HIGH，与 MIN_RELEVANCE=3 配合，
# 核心词单独命中即达入库线；三维/可视化 同时保留在 MEDIUM 形成叠加计分）
RELEVANCE_HIGH = ['数字孪生', '视频融合', '三维可视化', '三维引擎', '数字底座', '孪生底座', '时空底座',
                  '视频孪生', '三维', '可视化']
RELEVANCE_MEDIUM = ['智慧医院', '智慧校园', '智慧园区', '可视化', '三维', 'BIM', 'GIS', 'CIM', '孪生', '元宇宙']
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
