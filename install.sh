#!/usr/bin/env bash
set -Eeuo pipefail
APP_DIR="${APP_DIR:-/opt/cmcc-linux-docker}"
PORT="${CMCC_PORT:-8080}"
ASSET="CMCC.Docker.zip"
TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
[[ "$(id -u)" == 0 ]] || { echo '请使用 root 或 sudo 运行'; exit 1; }
SUDO=''
command -v sudo >/dev/null 2>&1 && [[ "$(id -u)" != 0 ]] && SUDO=sudo
install_packages() {
  local pkgs=("$@")
  if command -v apt-get >/dev/null 2>&1; then
    DEBIAN_FRONTEND=noninteractive apt-get update -y
    DEBIAN_FRONTEND=noninteractive apt-get install -y "${pkgs[@]}"
  elif command -v dnf >/dev/null 2>&1; then
    dnf install -y "${pkgs[@]}"
  elif command -v yum >/dev/null 2>&1; then
    yum install -y "${pkgs[@]}"
  elif command -v apk >/dev/null 2>&1; then
    apk add --no-cache "${pkgs[@]}"
  else
    echo '无法识别系统包管理器，请先安装 curl、unzip、python3 和 Docker Engine/Compose v2'; exit 1
  fi
}
if ! command -v curl >/dev/null 2>&1 || ! command -v unzip >/dev/null 2>&1 || ! command -v python3 >/dev/null 2>&1 || ! command -v find >/dev/null 2>&1; then
  echo '检测到缺少基础依赖，正在自动安装 curl unzip python3 findutils...'
  install_packages curl unzip python3 findutils 2>/dev/null || install_packages curl unzip python3
fi
if ! command -v docker >/dev/null 2>&1; then
  echo '未检测到 Docker，正在通过 Docker 官方安装脚本安装...'
  curl --http1.1 -fsSL --retry 5 --retry-all-errors --connect-timeout 30 --max-time 600 https://get.docker.com -o "$TMP/get-docker.sh"
  bash "$TMP/get-docker.sh"
  systemctl enable --now docker 2>/dev/null || service docker start 2>/dev/null || true
fi
if ! command -v docker >/dev/null 2>&1; then echo 'Docker 安装失败'; exit 1; fi
if ! docker compose version >/dev/null 2>&1; then
  echo '未检测到 Docker Compose v2，正在自动安装 Compose 插件...'
  if command -v apt-get >/dev/null 2>&1; then apt-get update -y && apt-get install -y docker-compose-plugin; \
  elif command -v dnf >/dev/null 2>&1; then dnf install -y docker-compose-plugin; \
  elif command -v yum >/dev/null 2>&1; then yum install -y docker-compose-plugin; \
  elif command -v apk >/dev/null 2>&1; then apk add --no-cache docker-cli-compose; fi
fi
docker compose version >/dev/null 2>&1 || { echo 'Docker Compose v2 安装失败'; exit 1; }
mkdir -p "$APP_DIR"
FOUND="${CMCC_LOCAL_ARCHIVE:-}"
if [[ -z "$FOUND" || ! -f "$FOUND" ]]; then
  for p in "$(pwd)/$ASSET" "/tmp/$ASSET" "/root/$ASSET" "/opt/$ASSET" "$APP_DIR/$ASSET"; do
    [[ -f "$p" ]] && { FOUND="$p"; break; }
  done
fi
ARCHIVE="$TMP/$ASSET"
if [[ -n "$FOUND" && -f "$FOUND" ]]; then
  echo "使用本地安装包：$(readlink -f "$FOUND")"; cp -f "$FOUND" "$ARCHIVE"
else
  echo '从 GitHub Release 获取安装包（仅访问 GitHub）'
  curl --http1.1 -fL --retry 5 --retry-all-errors --connect-timeout 30 --max-time 1800 --progress-bar --show-error \
    'https://github.com/952371672/linux-/releases/download/stable-latest/CMCC.Docker.zip' -o "$ARCHIVE"
fi
[[ -s "$ARCHIVE" ]] || { echo '安装包为空'; exit 1; }
unzip -tq "$ARCHIVE"
mkdir -p "$TMP/unpack"; unzip -q "$ARCHIVE" -d "$TMP/unpack"
DF="$(find "$TMP/unpack" -type f -name novnc-Dockerfile -print -quit)"; [[ -n "$DF" ]] || { echo '安装包缺少 novnc-Dockerfile'; exit 1; }
SRC="$(dirname "$DF")"; [[ -f "$SRC/novnc-compose.yml" ]] || exit 1
mkdir -p "$APP_DIR/data"
[[ -f "$APP_DIR/novnc-compose.yml" ]] && docker compose -f "$APP_DIR/novnc-compose.yml" down || true
[[ -f "$APP_DIR/docker-compose.yml" ]] && docker compose -f "$APP_DIR/docker-compose.yml" down || true
cp -a "$APP_DIR/.env" "$TMP/.env" 2>/dev/null || true
cp -a "$APP_DIR/data/webui-auth.json" "$TMP/webui-auth.json" 2>/dev/null || true
find "$APP_DIR" -mindepth 1 -maxdepth 1 ! -name data -exec rm -rf {} +
cp -a "$SRC"/. "$APP_DIR"/
cp -a "$TMP/.env" "$APP_DIR/.env" 2>/dev/null || true
cp -a "$TMP/webui-auth.json" "$APP_DIR/data/webui-auth.json" 2>/dev/null || true
if [[ ! -f "$APP_DIR/.env" ]]; then printf 'CMCC_WEBUI_USER=admin\nCMCC_WEBUI_PASSWORD=admin\n' > "$APP_DIR/.env"; fi
chmod 600 "$APP_DIR/.env" 2>/dev/null || true
[[ "$PORT" == 8080 ]] || sed -i "s/\"8080:8080\"/\"${PORT}:8080\"/" "$APP_DIR/novnc-compose.yml"
cd "$APP_DIR"; docker compose -f novnc-compose.yml config >/dev/null
docker compose -f novnc-compose.yml build
docker compose -f novnc-compose.yml up -d
sleep 5; docker compose -f novnc-compose.yml ps
echo 'GitHub安装完成（仅访问GitHub）'
