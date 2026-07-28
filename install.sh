#!/usr/bin/env bash
set -Eeuo pipefail

APP_DIR="${APP_DIR:-/opt/cmcc-linux-docker}"
REPO="${GITHUB_REPO:-952371672/linux-}"
ASSET="${GITHUB_ASSET:-CMCC.Docker.zip}"
TAG="${GITHUB_TAG:-v20260728-cmcc}"
PORT="${CMCC_PORT:-8080}"
ARCHIVE_URL="${CMCC_ARCHIVE_URL:-https://github.com/${REPO}/releases/download/%E6%AD%A3%E5%BC%8F%E7%89%88/${ASSET}}"

[[ "$(id -u)" == 0 ]] || { echo '请使用 root 或 sudo 运行'; exit 1; }
for cmd in curl unzip docker find python3; do command -v "$cmd" >/dev/null || { echo "缺少命令：$cmd"; exit 1; }; done
docker compose version >/dev/null 2>&1 || { echo '缺少 Docker Compose v2 插件'; exit 1; }

TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
ARCHIVE="$TMP/$ASSET"

echo '[1/7] 检查依赖和安装目录'
mkdir -p "$APP_DIR"

# 优先使用本机现有安装包，避免无外网时仍访问 GitHub。
FOUND="${CMCC_LOCAL_ARCHIVE:-}"
if [[ -z "$FOUND" || ! -f "$FOUND" ]]; then
  for candidate in "$(pwd)/$ASSET" "/tmp/$ASSET" "/root/$ASSET" "/opt/$ASSET" "$APP_DIR/$ASSET"; do
    if [[ -f "$candidate" ]]; then FOUND="$candidate"; break; fi
  done
fi
if [[ -n "$FOUND" && -f "$FOUND" ]]; then
  echo "[2/7] 使用本地安装包：$(readlink -f "$FOUND")"
  cp -f "$FOUND" "$ARCHIVE"
else
  echo "[2/7] 下载 GitHub Release：$ASSET"
  curl -fL --retry 3 --connect-timeout 20 --speed-time 60 --speed-limit 1024 --progress-bar --show-error -o "$ARCHIVE" "$ARCHIVE_URL"
fi

[[ -s "$ARCHIVE" ]] || { echo '安装包为空'; exit 1; }
echo '[3/7] 校验并解压安装包'
unzip -tq "$ARCHIVE"
rm -rf "$TMP/unpack"; mkdir -p "$TMP/unpack"
unzip -q "$ARCHIVE" -d "$TMP/unpack"
DOCKERFILE="$(find "$TMP/unpack" -type f -name novnc-Dockerfile -print -quit)"
[[ -n "$DOCKERFILE" ]] || { echo '安装包中找不到 novnc-Dockerfile'; exit 1; }
SRC="$(dirname "$DOCKERFILE")"

COMPOSE="$SRC/novnc-compose.yml"
[[ -f "$COMPOSE" ]] || { echo '安装包中找不到 novnc-compose.yml'; exit 1; }

echo '[4/7] 停止旧容器并保留运行数据'
if [[ -f "$APP_DIR/novnc-compose.yml" ]]; then docker compose -f "$APP_DIR/novnc-compose.yml" down || true; fi
if [[ -f "$APP_DIR/docker-compose.yml" ]]; then docker compose -f "$APP_DIR/docker-compose.yml" down || true; fi
mkdir -p "$APP_DIR/data"
BACKUP="$TMP/old-files"; mkdir -p "$BACKUP"
[[ -f "$APP_DIR/.env" ]] && cp -a "$APP_DIR/.env" "$BACKUP/.env"
[[ -f "$APP_DIR/data/webui-auth.json" ]] && cp -a "$APP_DIR/data/webui-auth.json" "$BACKUP/webui-auth.json"

find "$APP_DIR" -mindepth 1 -maxdepth 1 ! -name data -exec rm -rf {} +
cp -a "$SRC"/. "$APP_DIR"/
[[ -f "$BACKUP/.env" ]] && cp -a "$BACKUP/.env" "$APP_DIR/.env"
[[ -f "$BACKUP/webui-auth.json" ]] && cp -a "$BACKUP/webui-auth.json" "$APP_DIR/data/webui-auth.json"
chmod 600 "$APP_DIR/.env" 2>/dev/null || true

if [[ "$PORT" != 8080 ]]; then sed -i "s/\"8080:8080\"/\"${PORT}:8080\"/" "$APP_DIR/novnc-compose.yml"; fi

echo '[5/7] 检查 Docker 架构和配置'
case "$(uname -m)" in x86_64|amd64) echo 'amd64：可部署';; *) echo "警告：架构 $(uname -m)，官方客户端只验证过 amd64";; esac
cd "$APP_DIR"
docker compose -f novnc-compose.yml config >/dev/null

echo '[6/7] 构建镜像'
docker compose -f novnc-compose.yml build

echo '[7/7] 启动服务并健康检查'
docker compose -f novnc-compose.yml up -d
sleep 5
if curl -fsS -u "${CMCC_WEBUI_USER:-admin}:${CMCC_WEBUI_PASSWORD:-change-this-immediately}" "http://127.0.0.1:${PORT}/health" >/dev/null; then
  echo '部署完成，健康检查通过'
else
  echo '容器已启动但健康检查失败；请检查：docker compose -f novnc-compose.yml logs --tail=100' >&2
  exit 1
fi
echo "WebUI: http://$(hostname -I 2>/dev/null | awk '{print $1}'):${PORT}/"
echo "目录: $APP_DIR"
