#!/usr/bin/env bash
set -Eeuo pipefail

APP_DIR="${APP_DIR:-/opt/cmcc-linux-docker}"
GITHUB_REPO="${GITHUB_REPO:-952371672/linux-}"
GITHUB_TAG="${GITHUB_TAG:-1.1}"
GITHUB_ASSET="${GITHUB_ASSET:-CMCC.Linux_Docker._v1.1_WebUI.zip}"
DEFAULT_ARCHIVE_URL="https://github.com/${GITHUB_REPO}/releases/download/${GITHUB_TAG}/${GITHUB_ASSET}"
ARCHIVE_URL="${1:-${CMCC_ARCHIVE_URL:-$DEFAULT_ARCHIVE_URL}}"
PORT="${CMCC_PORT:-8080}"

if [[ "$(id -u)" != 0 ]]; then
  echo "请使用 root 或 sudo 运行"
  exit 1
fi

if [[ -z "${GITHUB_TOKEN:-}" ]]; then
  if [[ -t 0 ]]; then
    read -r -s -p "请输入 GitHub 只读 Token（不会显示）：" GITHUB_TOKEN
    echo
    export GITHUB_TOKEN
  else
    echo "私有仓库需要 GITHUB_TOKEN；请先交互式运行脚本并输入 Token" >&2
    exit 2
  fi
fi

for cmd in curl unzip docker python3; do
  command -v "$cmd" >/dev/null 2>&1 || { echo "缺少命令：$cmd"; exit 1; }
done
if ! docker compose version >/dev/null 2>&1; then
  echo "缺少 Docker Compose v2 插件，请先安装 Docker Compose"
  exit 1
fi

TMP="$(mktemp -d)"
cleanup(){ rm -rf "$TMP"; }
trap cleanup EXIT
ARCHIVE="$TMP/cmcc.zip"
echo "[1/6] 下载项目：$ARCHIVE_URL"
if [[ -n "${GITHUB_TOKEN:-}" ]]; then
  echo "使用 GitHub Token 下载私有 Release"
  API_URL="https://api.github.com/repos/${GITHUB_REPO}/releases/tags/${GITHUB_TAG}"
  RELEASE_JSON="$TMP/release.json"
  curl -fsSL --retry 3 --connect-timeout 15 \
    -H "Authorization: Bearer ${GITHUB_TOKEN}" \
    -H "Accept: application/vnd.github+json" \
    -H "X-GitHub-Api-Version: 2022-11-28" \
    -o "$RELEASE_JSON" "$API_URL"
  ASSET_API_URL="$(python3 - "$RELEASE_JSON" "$GITHUB_ASSET" <<'PY'
import json,sys
p,name=sys.argv[1:]
data=json.load(open(p,encoding='utf-8'))
for asset in data.get('assets',[]):
    if asset.get('name')==name:
        print(asset['url']); break
else:
    raise SystemExit('Release 中找不到附件: '+name)
PY
)"
  curl -fL --retry 3 --connect-timeout 15 \
    -H "Authorization: Bearer ${GITHUB_TOKEN}" \
    -H "Accept: application/octet-stream" \
    -H "X-GitHub-Api-Version: 2022-11-28" \
    -o "$ARCHIVE" "$ASSET_API_URL"
else
  echo "未提供 GITHUB_TOKEN，按公开 Release 地址下载"
  curl -fL --retry 3 --connect-timeout 15 -o "$ARCHIVE" "$ARCHIVE_URL"
fi

echo "[2/6] 准备目录：$APP_DIR"
mkdir -p "$APP_DIR"
if [[ -f "$APP_DIR/docker-compose.yml" ]]; then
  docker compose -f "$APP_DIR/docker-compose.yml" down || true
fi
rm -rf "$TMP/project"
mkdir -p "$TMP/project"
unzip -q "$ARCHIVE" -d "$TMP/project"

# 支持压缩包根目录或单层目录两种格式
SRC="$TMP/project"
if [[ ! -f "$SRC/docker-compose.yml" ]]; then
  CANDIDATE="$(find "$SRC" -mindepth 1 -maxdepth 2 -name docker-compose.yml -print -quit)"
  [[ -n "$CANDIDATE" ]] || { echo "压缩包中找不到 docker-compose.yml"; exit 1; }
  SRC="$(dirname "$CANDIDATE")"
fi

echo "[3/6] 更新项目文件"
# 保留已有 data，避免覆盖账号、密钥和 profile
mkdir -p "$APP_DIR/data"
find "$APP_DIR" -mindepth 1 -maxdepth 1 ! -name data -exec rm -rf {} +
cp -a "$SRC"/. "$APP_DIR"/
mkdir -p "$APP_DIR/data"

# 允许通过 CMCC_PORT 修改宿主机端口，容器内部仍监听 8080
if [[ "$PORT" != "8080" ]]; then
  sed -i "s/\"8080:8080\"/\"${PORT}:8080\"/" "$APP_DIR/docker-compose.yml"
fi

echo "[4/6] 检查 Docker 架构"
ARCH="$(uname -m)"
case "$ARCH" in
  x86_64|amd64) echo "amd64：可部署";;
  *) echo "警告：当前架构为 $ARCH，本项目官方客户端只验证过 amd64";;
esac

echo "[5/6] 构建镜像"
cd "$APP_DIR"
docker compose build

echo "[6/6] 启动服务"
docker compose up -d
sleep 3
if ! curl -fsS "http://127.0.0.1:${PORT}/health"; then
  echo
  echo "容器已启动但健康检查失败，请执行：docker compose -f $APP_DIR/docker-compose.yml logs --tail=100"
  exit 1
fi

echo
echo "部署完成"
echo "WebUI: http://$(hostname -I 2>/dev/null | awk '{print $1}'):${PORT}/"
echo "目录: $APP_DIR"
echo "日志: cd $APP_DIR && docker compose logs -f"
