FROM node:22-bookworm-slim AS channel-node

FROM python:3.12-slim

WORKDIR /workspace

ENV PYTHONUNBUFFERED=1
ENV PYTHONPATH=/workspace/apps/api

ARG CGA_GPU=false
ARG CGA_TORCH_INDEX_URL=

RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY apps/api/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt \
    && if [ "$CGA_GPU" = "true" ]; then \
        test -n "$CGA_TORCH_INDEX_URL"; \
        pip install --no-cache-dir --index-url "$CGA_TORCH_INDEX_URL" torch; \
    fi

ENV PYTHONDONTWRITEBYTECODE=1

COPY --from=channel-node /usr/local/bin/node /usr/local/bin/node
COPY packages/shared/src/channel.ts packages/shared/src/channel-bridge.mjs /workspace/packages/shared/src/
COPY apps/api /workspace/apps/api
RUN find /workspace/apps/api -type d -name '__pycache__' -prune -exec rm -rf {} + \
    && find /workspace/apps/api -type f -name '*.py[co]' -delete \
    && chmod +x /workspace/apps/api/scripts/start-cga-api.sh

EXPOSE 8000

CMD ["/workspace/apps/api/scripts/start-cga-api.sh"]
