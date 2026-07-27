#!/usr/bin/env bash
set -Eeuo pipefail

# CMCC Cloud Alive one-click installer
# Usage:
#   curl -fsSL https://raw.githubusercontent.com/OWNER/REPO/main/install.sh | bash -s -- OWNER/REPO
# Optional:
#   INSTALL_DIR=/opt/cmcc-linux-docker CMCC_ASSET=CMCC.Docker.zip bash -s -- OWNER/REPO

REPO="${1:-${CMCC_GITHUB_REPO:-}}"
ASSET="${CMCC_ASSET:-CMCC.Docker.zip}"
INSTALL_DIR="${INSTALL_DIR:-/opt/cmcc-linux-docker}"
RELEASE_TAG="${CMCC_RELEASE_TAG:-latest}"

if [[ -z "$REPO" || ! "$REPO" =~ ^[^/]+/[^/]+$ ]]; then
  echo "用法：curl -fsSL https://raw.githubusercontent.com/OWNER/REPO/main/install.sh | bash -s -- OWNER/REPO" >&2
  exit 2
fi

for cmd in curl unzip sha256sum awk sed grep python3; do
  command -v "$cmd" >/dev/null 2>&1 || { echo "缺少命令：$cmd" >&2; exit 1; }
done

if ! command -v docker >/dev/null 2>&1; then
  echo "未检测到 Docker，正在安装 Docker Engine..."
  curl -fsSL https://get.docker.com | sh
fi

if ! docker compose version >/dev/null 2>&1; then
  echo "当前 Docker 缺少 Compose v2，请先安装 docker compose 插件。" >&2
  exit 1
fi

if [[ "$RELEASE_TAG" == "latest" ]]; then
  API_URL="https://api.github.com/repos/${REPO}/releases/latest"
else
  API_URL="https://api.github.com/repos/${REPO}/releases/tags/${RELEASE_TAG}"
fi

TMP_DIR="$(mktemp -d)"
cleanup() { rm -rf "$TMP_DIR"; }
trap cleanup EXIT

JSON="$TMP_DIR/release.json"
echo "读取 GitHub Release：${REPO} (${RELEASE_TAG})"
curl -fsSL -H 'Accept: application/vnd.github+json' "$API_URL" -o "$JSON"

DOWNLOAD_URL="$(python3 - "$JSON" "$ASSET" <<'PY'
import json, sys
with open(sys.argv[1], encoding='utf-8') as f:
    data=json.load(f)
for asset in data.get('assets', []):
    if asset.get('name') == sys.argv[2]:
        print(asset.get('browser_download_url',''))
        break
PY
)"
if [[ -z "$DOWNLOAD_URL" ]]; then
  echo "Release 中找不到资源：$ASSET" >&2
  echo "可用资源：" >&2
  grep -o '"name"[[:space:]]*:[[:space:]]*"[^"]*"' "$JSON" | sed -E 's/.*"([^"]+)"/  \1/' >&2 || true
  exit 1
fi

ARCHIVE="$TMP_DIR/package.zip"
echo "下载：$ASSET"
curl -fL --retry 3 --retry-delay 2 --progress-bar "$DOWNLOAD_URL" -o "$ARCHIVE"
unzip -tq "$ARCHIVE"

STAGE="$TMP_DIR/stage"
mkdir -p "$STAGE"
unzip -q "$ARCHIVE" -d "$STAGE"
ROOT="$STAGE/CMCC云电脑保活Docker版"
[[ -d "$ROOT" ]] || ROOT="$STAGE"

for required in novnc-Dockerfile novnc-compose.yml novnc-entrypoint.sh service.py webui/index.html; do
  [[ -e "$ROOT/$required" ]] || { echo "安装包缺少文件：$required" >&2; exit 1; }
done

if [[ -e "$INSTALL_DIR/data/accounts.json" || -e "$INSTALL_DIR/data/.secret" ]]; then
  BACKUP="${INSTALL_DIR}.backup.$(date +%Y%m%d%H%M%S)"
  echo "发现已有运行数据，先备份到：$BACKUP"
  mkdir -p "$BACKUP"
  cp -a "$INSTALL_DIR/data" "$BACKUP/"
  [[ -e "$INSTALL_DIR/.env" ]] && cp -a "$INSTALL_DIR/.env" "$BACKUP/"
fi

mkdir -p "$INSTALL_DIR"
cp -a "$ROOT"/. "$INSTALL_DIR"/
mkdir -p "$INSTALL_DIR/data/profiles"
chmod 700 "$INSTALL_DIR/data/profiles"

if [[ ! -e "$INSTALL_DIR/.env" ]]; then
  if [[ -e "$INSTALL_DIR/.env.example" ]]; then
    cp "$INSTALL_DIR/.env.example" "$INSTALL_DIR/.env"
  else
    cat > "$INSTALL_DIR/.env" <<'EOF'
CMCC_WEBUI_USER=admin
CMCC_WEBUI_PASSWORD=change-this-immediately
EOF
  fi
  chmod 600 "$INSTALL_DIR/.env"
  echo "已创建 $INSTALL_DIR/.env，请尽快修改 CMCC_WEBUI_PASSWORD。"
fi

cd "$INSTALL_DIR"
docker compose -f novnc-compose.yml up -d --build
docker compose -f novnc-compose.yml ps

echo
echo "安装完成：$INSTALL_DIR"
echo "WebUI：http://$(hostname -I 2>/dev/null | awk '{print $1}') :8080"
echo "查看日志：cd $INSTALL_DIR && docker compose -f novnc-compose.yml logs --tail 100 -f"
