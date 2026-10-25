FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1
WORKDIR /app

RUN apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install --yes --no-install-recommends tzdata ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 suikouji \
    && useradd --uid 10001 --gid 10001 --no-create-home --shell /usr/sbin/nologin suikouji

COPY engine/ ./engine/
COPY channel/ ./channel/
COPY web/demo.html ./web/demo.html

USER 10001:10001
EXPOSE 8765
ENTRYPOINT ["python3", "-m", "engine.deployment"]
CMD ["serve"]
