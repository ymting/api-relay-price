# 运行镜像只带标准库应用，不装 Node / 浏览器 / 测试工具。
FROM python:3.12-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PYTHONUTF8=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TZ=Asia/Shanghai \
    RELAY_MODE=server \
    RELAY_HOST=0.0.0.0 \
    RELAY_PORT=8765 \
    RELAY_STRICT_PORT=1 \
    RELAY_STATE_DIR=/var/lib/relay-price

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates tzdata \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 relay \
    && useradd --uid 10001 --gid 10001 --no-create-home --shell /usr/sbin/nologin relay \
    && install -d -m 0700 -o relay -g relay /var/lib/relay-price

WORKDIR /app
COPY server.py fetch_prices.py VERSION ./
COPY integrations/ ./integrations/
COPY 中转站比价.html ./中转站比价.html
COPY sites.example.json ./sites.example.json

USER 10001:10001
VOLUME ["/var/lib/relay-price"]
EXPOSE 8765

# shell 只用于设置 umask，必须 exec 才能正确接收 SIGTERM。
CMD ["sh", "-c", "umask 0077; exec python -X utf8 server.py --port 8765"]
