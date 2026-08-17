#!/usr/bin/env bash
set -Eeuo pipefail
APP_DIR="${APP_DIR:-/opt/cmcc-linux-docker}"
PORT="${CMCC_PORT:-8080}"
CNB_IMAGE="${CNB_IMAGE:-docker.cnb.cool/952371672/cmcc-linux-docker:stable-latest}"
ASSET="CMCC.Docker.zip"
DOCKER_INSTALL_URL="${DOCKER_INSTALL_URL:-https://get.docker.com}"
DOCKER_MIRRORS="${DOCKER_MIRRORS:-https://docker.m.daocloud.io,https://dockerproxy.net,https://docker.1ms.run}"
TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
step() { printf '\n==== [%s] %s ====\n' "$1" "$2"; }
progress_download() { local url="$1" out="$2"; shift 2; local part="${out}.part"; rm -f "$out" "$part"; curl --http1.1 -fL --retry 5 --retry-all-errors --retry-delay 2 --connect-timeout 30 --speed-time 60 --speed-limit 1024 --max-time 1800 --progress-bar --show-error "$@" "$url" -o "$part"; [[ -s "$part" ]] || { echo '下载结果为空' >&2; return 1; }; mv -f "$part" "$out"; printf '\n下载完成：%s\n' "$out"; }
step 1 '检查运行权限与系统依赖'
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
step 2 '检查并安装 Docker / Compose'
if ! command -v docker >/dev/null 2>&1; then
  step 2 '安装 Docker / Compose（优先国内软件源）'
  DOCKER_INSTALLED=0
  if command -v apt-get >/dev/null 2>&1; then
    echo '尝试 Ubuntu/Debian 国内软件源 docker.io + compose v2...'
    apt-get update -y && DEBIAN_FRONTEND=noninteractive apt-get install -y docker.io docker-compose-v2 docker-buildx 2>/dev/null && DOCKER_INSTALLED=1 || true
    if [[ "$DOCKER_INSTALLED" != 1 ]]; then
      echo 'docker-compose-v2 不可用，尝试 docker-compose-plugin...'
      DEBIAN_FRONTEND=noninteractive apt-get install -y docker.io docker-compose-plugin docker-buildx 2>/dev/null && DOCKER_INSTALLED=1 || true
    fi
  elif command -v dnf >/dev/null 2>&1; then
    dnf install -y moby-engine docker-compose-plugin docker-buildx-plugin 2>/dev/null && DOCKER_INSTALLED=1 || true
  elif command -v yum >/dev/null 2>&1; then
    yum install -y docker docker-compose-plugin 2>/dev/null && DOCKER_INSTALLED=1 || true
  elif command -v apk >/dev/null 2>&1; then
    apk add --no-cache docker docker-cli-compose 2>/dev/null && DOCKER_INSTALLED=1 || true
  fi
  if [[ "$DOCKER_INSTALLED" != 1 ]]; then
    echo '系统软件源未提供 Docker，尝试 Docker 官方脚本（失败不会执行半截脚本）'
    rm -f "$TMP/get-docker.sh"
    if curl --http1.1 -fSL --retry 5 --retry-all-errors --connect-timeout 30 --max-time 600 "$DOCKER_INSTALL_URL" -o "$TMP/get-docker.sh"; then
      bash "$TMP/get-docker.sh" && DOCKER_INSTALLED=1 || true
    else
      echo 'Docker 官方安装脚本下载失败'
    fi
  fi
  systemctl enable --now docker 2>/dev/null || service docker start 2>/dev/null || true
fi
if ! command -v docker >/dev/null 2>&1; then echo 'Docker 安装失败：请检查 apt/dnf/yum 网络或手动安装 docker.io'; exit 1; fi
if command -v docker >/dev/null 2>&1 && [[ -n "$DOCKER_MIRRORS" ]]; then
  echo "配置 Docker 国内镜像源：$DOCKER_MIRRORS"
  mkdir -p /etc/docker
  python3 - "$DOCKER_MIRRORS" <<'PY'
import json,os,sys,tempfile
p='/etc/docker/daemon.json'; raw={}
try:
 with open(p,encoding='utf8') as f: raw=json.load(f)
except (FileNotFoundError,json.JSONDecodeError): pass
mirrors=[x for x in sys.argv[1].split(',') if x]
old=raw.get('registry-mirrors',[])
raw['registry-mirrors']=list(dict.fromkeys(old+mirrors))
d=os.path.dirname(p); fd,tmp=tempfile.mkstemp(prefix='.daemon.',dir=d,text=True)
with os.fdopen(fd,'w',encoding='utf8') as f: json.dump(raw,f,ensure_ascii=False,indent=2); f.write('\n')
os.replace(tmp,p)
PY
  systemctl daemon-reload 2>/dev/null || true
  systemctl restart docker 2>/dev/null || service docker restart 2>/dev/null || true
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
step 3 '准备安装目录'
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
  echo '从 CNB 公开制品获取安装包（无需登录和Token）'
  REGISTRY="https://${CNB_IMAGE%%/*}"
  REF="${CNB_IMAGE#*/}"; TAG="${REF##*:}"; REPO="${REF%:*}"
  TOKEN_URL="$REGISTRY/service/token?service=cnb-registry&scope=repository:${REPO}:pull"
  curl --http1.1 -fsSL --retry 5 --retry-all-errors --connect-timeout 30 --progress-bar --show-error "$TOKEN_URL" -o "$TMP/token.json"
  AUTH_VALUE="$(python3 - "$TMP/token.json" <<'PY'
import json,sys
p=json.load(open(sys.argv[1],encoding='utf8'))
print(p.get('token') or p.get('access_token') or '')
PY
)"
  [[ -n "$AUTH_VALUE" ]] || { echo 'CNB匿名制品Token获取失败'; exit 1; }
  printf 'Authorization: Bearer %s\n' "$AUTH_VALUE" > "$TMP/auth.header"
  curl --http1.1 -fsSL --retry 5 --retry-all-errors --connect-timeout 30 --progress-bar --show-error -H "@${TMP}/auth.header" -H 'Accept: application/vnd.oci.image.manifest.v1+json' "$REGISTRY/v2/$REPO/manifests/$TAG" -o "$TMP/manifest.json"
  LAYER="$(python3 - "$TMP/manifest.json" <<'PY'
import json,sys
p=json.load(open(sys.argv[1],encoding='utf8'))
for x in p.get('layers',[]):
 if x.get('annotations',{}).get('org.opencontainers.image.title')=='CMCC.Docker.zip' or x.get('mediaType')=='application/zip':
  print(x['digest']); break
else: raise SystemExit('CNB制品中找不到 CMCC.Docker.zip 层')
PY
)"
  [[ "$LAYER" == sha256:* ]] || exit 1
  step 4 '从 CNB 下载完整安装包（显示实时进度）'
  progress_download "$REGISTRY/v2/$REPO/blobs/$LAYER" "$ARCHIVE" -H "@${TMP}/auth.header"
  printf '\nCNB 安装包下载完成：%s bytes\n' "$(stat -c %s "$ARCHIVE" 2>/dev/null || wc -c < "$ARCHIVE")"
fi
step 5 '校验并解压安装包'
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
step 6 '构建 Docker 镜像（显示构建进度）'
cd "$APP_DIR"; docker compose -f novnc-compose.yml config >/dev/null
docker compose -f novnc-compose.yml build --progress=plain
step 7 '启动服务并检查状态'
docker compose -f novnc-compose.yml up -d
sleep 5; docker compose -f novnc-compose.yml ps
WEBUI_USER="$(sed -n 's/^CMCC_WEBUI_USER=//p' "$APP_DIR/.env" | tail -n 1)"; WEBUI_PASSWORD="$(sed -n 's/^CMCC_WEBUI_PASSWORD=//p' "$APP_DIR/.env" | tail -n 1)"
printf '\n========================================\nCNB 安装完成\nWebUI 登录地址：http://服务器IP:%s/\nWebUI 用户名：%s\nWebUI 密码：%s\n========================================\n' "$PORT" "${WEBUI_USER:-admin}" "${WEBUI_PASSWORD:-admin}"
echo '请首次登录后立即修改密码；以上凭据不会写入日志。'
