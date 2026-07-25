#!/usr/bin/env bash
set -Eeuo pipefail

# CMCC Linux Docker WebUI 一键安装器
# 从私有 GitHub Release 下载项目 ZIP；不会把 Token 写入文件或镜像。

GITHUB_REPO="${GITHUB_REPO:-952371672/linux-}"
GITHUB_TAG="${GITHUB_TAG:-1.1}"
GITHUB_ASSET="${GITHUB_ASSET:-CMCC.Linux_Docker._v1.1_WebUI.zip}"
APP_DIR="${APP_DIR:-/opt/cmcc-linux-docker}"
PORT="${CMCC_PORT:-8080}"

if [[ "$(id -u)" != 0 ]]; then
  echo "请使用 root 运行：sudo -i 后再执行此脚本" >&2
  exit 1
fi

if [[ -z "${GITHUB_TOKEN:-}" ]]; then
  if [[ -t 0 ]]; then
    read -r -s -p "请输入 GitHub Token（不会显示）：" GITHUB_TOKEN
    echo
    export GITHUB_TOKEN
  else
    echo "未检测到 GITHUB_TOKEN，无法访问私有 Release" >&2
    echo "请先执行：export GITHUB_TOKEN='你的只读Token'" >&2
    exit 2
  fi
fi

for cmd in curl unzip docker python3; do
  command -v "$cmd" >/dev/null 2>&1 || {
    echo "缺少命令：$cmd" >&2
    exit 1
  }
done
if ! docker compose version >/dev/null 2>&1; then
  echo "缺少 Docker Compose v2 插件" >&2
  exit 1
fi

TMP="$(mktemp -d)"
cleanup() { rm -rf "$TMP"; }
trap cleanup EXIT

TOKEN_HEADER="Authorization: Bearer "
TOKEN_HEADER+="${GITHUB_TOKEN}"
AUTH=(-H "$TOKEN_HEADER" -H "Accept: application/vnd.github+json" -H "X-GitHub-Api-Version: 2022-11-28")
API="https://api.github.com/repos/${GITHUB_REPO}"
RELEASE_JSON="$TMP/release.json"
ARCHIVE="$TMP/cmcc.zip"

step() { echo; echo "[$1/6] $2"; }

step 1 "读取私有 Release：${GITHUB_TAG}"
HTTP="$(curl -sS -o "$RELEASE_JSON" -w '%{http_code}' "${AUTH[@]}" "$API/releases/tags/$GITHUB_TAG")"
if [[ "$HTTP" != "200" ]]; then
  echo "读取 Release 失败，HTTP=$HTTP" >&2
  python3 - "$RELEASE_JSON" <<'PY' || true
import json,sys
try:
    d=json.load(open(sys.argv[1],encoding='utf-8'))
    print('GitHub:', d.get('message','unknown error'))
except Exception:
    pass
PY
  echo "请确认仓库、Release 标签和 Token 权限：${GITHUB_REPO} / ${GITHUB_TAG}" >&2
  exit 1
fi

ASSET_API_URL="$(python3 - "$RELEASE_JSON" "$GITHUB_ASSET" <<'PY'
import json,sys
path,name=sys.argv[1:]
data=json.load(open(path,encoding='utf-8'))
for asset in data.get('assets',[]):
    if asset.get('name') == name:
        print(asset['url'])
        break
else:
    print('Release assets:', file=sys.stderr)
    for asset in data.get('assets',[]):
        print(' -', asset.get('name'), file=sys.stderr)
    raise SystemExit('找不到附件: '+name)
PY
)"

step 2 "下载项目附件：${GITHUB_ASSET}"
HTTP="$(curl --fail --location --show-error --progress-bar -o "$ARCHIVE" -w '%{http_code}' \
  -H "$TOKEN_HEADER" \
  -H "Accept: application/octet-stream" \
  -H "X-GitHub-Api-Version: 2022-11-28" \
  "$ASSET_API_URL")"
[[ "$HTTP" == "200" ]] || { echo "下载附件失败，HTTP=$HTTP" >&2; exit 1; }
unzip -tq "$ARCHIVE"

step 3 "检查本机环境"
case "$(uname -m)" in
  x86_64|amd64) echo "架构：amd64，继续安装";;
  *) echo "警告：架构为 $(uname -m)，官方客户端只验证过 amd64";;
esac

step 4 "解压到 ${APP_DIR}"
mkdir -p "$APP_DIR/data"
if [[ -f "$APP_DIR/docker-compose.yml" ]]; then
  docker compose -f "$APP_DIR/docker-compose.yml" down || true
fi
rm -rf "$TMP/project"
mkdir -p "$TMP/project"
unzip -q "$ARCHIVE" -d "$TMP/project"
SRC="$TMP/project"
if [[ ! -f "$SRC/docker-compose.yml" ]]; then
  CANDIDATE="$(find "$SRC" -mindepth 1 -maxdepth 3 -name docker-compose.yml -print -quit)"
  [[ -n "$CANDIDATE" ]] || { echo "压缩包中找不到 docker-compose.yml" >&2; exit 1; }
  SRC="$(dirname "$CANDIDATE")"
fi
find "$APP_DIR" -mindepth 1 -maxdepth 1 ! -name data -exec rm -rf {} +
cp -a "$SRC"/. "$APP_DIR"/
mkdir -p "$APP_DIR/data"
if [[ "$PORT" != "8080" ]]; then
  sed -i "s/\"8080:8080\"/\"${PORT}:8080\"/" "$APP_DIR/docker-compose.yml"
fi

step 5 "构建 Docker 镜像"
cd "$APP_DIR"
docker compose build

step 6 "启动并检查 WebUI"
docker compose up -d
sleep 5
if ! curl -fsS "http://127.0.0.1:${PORT}/health"; then
  echo
  echo "健康检查失败，请查看：cd ${APP_DIR} && docker compose logs --tail=100" >&2
  exit 1
fi

echo
echo "安装完成"
echo "WebUI：http://$(hostname -I 2>/dev/null | awk '{print $1}'):${PORT}/"
echo "安装目录：${APP_DIR}"
echo "查看日志：cd ${APP_DIR} && docker compose logs -f"
echo "注意：安装结束后可执行 unset GITHUB_TOKEN 清除 Token"
