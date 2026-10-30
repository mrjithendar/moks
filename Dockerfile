# syntax=docker/dockerfile:1.7
# One image for the API (moks serve), the worker (moks worker) and migrations (moks migrate).
# Build context: repository root. Built natively on arm64 for mk4 (see .github/workflows/ci.yml).

FROM node:26-alpine AS web
WORKDIR /web
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY frontend/ ./
# Must match MOKS_BASE_PATH at runtime.
ARG MOKS_BASE_PATH=/moks
RUN MOKS_BASE_PATH=$MOKS_BASE_PATH npm run build

FROM python:3.12-slim AS build
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /src
COPY backend/requirements.txt ./
RUN pip install --prefix=/install -r requirements.txt
COPY backend/ ./
RUN pip install --prefix=/install --no-deps .

FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 \
    MOKS_STATIC_DIR=/app/static MOKS_CREDENTIALS_DIR=/var/run/moks-credentials
RUN groupadd --gid 10001 moks && useradd --uid 10001 --gid 10001 --no-create-home --shell /usr/sbin/nologin moks
COPY --from=build /install /usr/local
COPY --from=web /web/dist /app/static
USER 10001:10001
EXPOSE 8080 9464
ENTRYPOINT ["moks"]
CMD ["serve"]
