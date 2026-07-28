#!/usr/bin/env bash
set -Eeuo pipefail
APP_DIR="${APP_DIR:-/opt/cmcc-linux-docker}"
ASSET="${GITHUB_ASSET:-CMCC.Docker.fix1.zip}"
TAG="${GITHUB_TAG:-v20260728-cmcc-fix1}"
PORT="${CMCC_PORT:-8080}"
URL="${CMCC_ARCHIVE_URL:-https://github.com/952371672/linux-/releases/download/${TAG}/${ASSET}}"
[[ "$(id -u)" == 0 ]] || { echo '请使用 root 或 sudo 运行'; exit 1; }
for cmd in curl unzip docker find; do command -v "$cmd" >/dev/null || { echo "缺少命令：$cmd"; exit 1; }; done
docker compose version >/dev/null 2>&1 || { echo '缺少 Docker Compose v2 插件'; exit 1; }
TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
ARCHIVE="$TMP/$ASSET"
echo '[1/7] 检查依赖和目录'; mkdir -p "$APP_DIR"
FOUND="${CMCC_LOCAL_ARCHIVE:-}"
if [[ -z "$FOUND" || ! -f "$FOUND" ]]; then
  for p in "$(pwd)/$ASSET" "/tmp/$ASSET" "/root/$ASSET" "/opt/$ASSET" "$APP_DIR/$ASSET"; do [[ -f "$p" ]] && { FOUND="$p"; break; }; done
fi
if [[ -n "$FOUND" && -f "$FOUND" ]]; then echo "[2/7] 使用本地安装包：$(readlink -f "$FOUND")"; cp -f "$FOUND" "$ARCHIVE"; else echo "[2/7] 下载：$URL"; curl -fL --retry 3 --connect-timeout 20 --speed-time 60 --speed-limit 1024 --progress-bar --show-error -o "$ARCHIVE" "$URL"; fi
[[ -s "$ARCHIVE" ]] || { echo '安装包为空'; exit 1; }
echo '[3/7] 校验并解压'; unzip -tq "$ARCHIVE"; mkdir -p "$TMP/unpack"; unzip -q "$ARCHIVE" -d "$TMP/unpack"; DF="$(find "$TMP/unpack" -type f -name novnc-Dockerfile -print -quit)"; [[ -n "$DF" ]] || { echo '找不到 novnc-Dockerfile'; exit 1; }; SRC="$(dirname "$DF")"; [[ -f "$SRC/novnc-compose.yml" ]] || { echo '找不到 novnc-compose.yml'; exit 1; }
echo '[4/7] 停止旧容器并备份配置'; mkdir -p "$APP_DIR/data"; mkdir -p "$TMP/backup"; [[ -f "$APP_DIR/.env" ]] && cp -a "$APP_DIR/.env" "$TMP/backup/.env" || true; [[ -f "$APP_DIR/data/webui-auth.json" ]] && cp -a "$APP_DIR/data/webui-auth.json" "$TMP/backup/webui-auth.json" || true; [[ -f "$APP_DIR/novnc-compose.yml" ]] && docker compose -f "$APP_DIR/novnc-compose.yml" down || true; [[ -f "$APP_DIR/docker-compose.yml" ]] && docker compose -f "$APP_DIR/docker-compose.yml" down || true
find "$APP_DIR" -mindepth 1 -maxdepth 1 ! -name data -exec rm -rf {} +; cp -a "$SRC"/. "$APP_DIR"/; [[ -f "$TMP/backup/.env" ]] && cp -a "$TMP/backup/.env" "$APP_DIR/.env" || true; [[ -f "$TMP/backup/webui-auth.json" ]] && cp -a "$TMP/backup/webui-auth.json" "$APP_DIR/data/webui-auth.json" || true; chmod 600 "$APP_DIR/.env" 2>/dev/null || true
[[ "$PORT" == 8080 ]] || sed -i "s/\"8080:8080\"/\"${PORT}:8080\"/" "$APP_DIR/novnc-compose.yml"
echo '[5/7] 检查配置'; cd "$APP_DIR"; docker compose -f novnc-compose.yml config >/dev/null
echo '[6/7] 构建镜像'; docker compose -f novnc-compose.yml build
echo '[7/7] 启动并检查'; docker compose -f novnc-compose.yml up -d; sleep 5; docker compose -f novnc-compose.yml ps; echo '更新完成。请用 WebUI 账号访问 /health 验证。'
