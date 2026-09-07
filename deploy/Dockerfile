FROM python:3.12-slim

WORKDIR /app

# 先装依赖，利用镜像层缓存
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY domain_monitor/ ./domain_monitor/
COPY config.example.yaml .

# 配置和数据都挂在这里
VOLUME ["/data"]
ENV DOMAIN_MONITOR_CONFIG=/data/config.yaml \
    PYTHONUNBUFFERED=1

# 容器里跑非 root
RUN useradd --create-home --uid 10001 monitor && chown -R monitor /app
USER monitor

ENTRYPOINT ["python", "-m", "domain_monitor"]
CMD ["run"]
