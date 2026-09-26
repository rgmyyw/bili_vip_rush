# B站联合会员抢购 - 运行镜像
# 构建后默认执行 --check；docker run/compose 可传任意 CLI 参数。
FROM python:3.12-slim

# B站活动按北京时间开售，容器必须固定时区，否则 --at/--daemon 的
# 本地时刻解析会偏 8 小时
ENV TZ=Asia/Shanghai \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends tzdata \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY config/ config/
COPY core/ core/
COPY flows/ flows/
COPY tools/ tools/
COPY main.py .

# 日志目录挂载点（compose 已挂 ./logs，便于宿主机 replay 复盘）
VOLUME ["/app/logs"]

ENTRYPOINT ["python", "main.py"]
CMD ["--check"]
