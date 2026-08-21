# 行业情报雷达 - CloudBase 云托管镜像
# 从 GitHub 导入源码后自动构建，监听 80 端口（云托管默认）
FROM python:3.11-slim

ENV TZ=Asia/Shanghai \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# CloudBase 云托管容器监听 80 端口
ENV PORT=80
EXPOSE 80

CMD ["gunicorn", "app:app", "--bind", "0.0.0.0:80", "--timeout", "120", "--workers", "2"]
