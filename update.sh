#!/usr/bin/env bash
set -Eeuo pipefail
APP_DIR="${APP_DIR:-/opt/cmcc-linux-docker}"
PORT="${CMCC_PORT:-8080}"
REPO="${GITHUB_REPO:-952371672/linux-}"
ASSET="${GITHUB_ASSET:-CMCC.Docker.zip}"
API="https://api.github.com/repos/${REPO}/releases/latest"
TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
[[ "$(id -u)" == 0 ]] || { echo '请使用 root 或 sudo 运行'; exit 1; }
for c in curl unzip docker find python3; do command -v "$c" >/dev/null || { echo "缺少命令：$c"; exit 1; }; done
docker compose version >/dev/null 2>&1 || { echo '缺少 Docker Compose v2 插件'; exit 1; }
echo '[1/7] 检查依赖和目录'; mkdir -p "$APP_DIR"
FOUND="${CMCC_LOCAL_ARCHIVE:-}"
if [[ -z "$FOUND" || ! -f "$FOUND" ]]; then for p in "$(pwd)/$ASSET" "/tmp/$ASSET" "/root/$ASSET" "/opt/$ASSET" "$APP_DIR/$ASSET"; do [[ -f "$p" ]] && { FOUND="$p"; break; }; done; fi
ARCHIVE="$TMP/$ASSET"
RELEASE_MARKER="$APP_DIR/data/.cmcc-release.json"
if [[ -n "$FOUND" && -f "$FOUND" ]]; then
 echo "[2/7] 使用本地安装包：$(readlink -f "$FOUND")"; cp -f "$FOUND" "$ARCHIVE"
else
 echo '[2/7] 检查 GitHub 最新版本（只下载元数据，不重复下载相同版本）'
 curl --http1.1 -fsSL --retry 5 --retry-all-errors --retry-delay 2 --connect-timeout 30 "$API" -o "$TMP/release.json"
 META="$(python3 - "$TMP/release.json" "$ASSET" <<'PY'
import json,sys
p=json.load(open(sys.argv[1],encoding='utf8'))
for a in p.get('assets',[]):
 if a.get('name')==sys.argv[2]:
  print(json.dumps({'id':a.get('id'),'size':a.get('size'),'updated_at':a.get('updated_at'),'url':a.get('browser_download_url','')},ensure_ascii=False)); break
else: raise SystemExit('最新 Release 中找不到 '+sys.argv[2])
PY
)"
 [[ -n "$META" ]] || exit 1
 if [[ -f "$RELEASE_MARKER" ]] && python3 - "$RELEASE_MARKER" "$META" <<'PY'
import json,sys
try:
 old=json.load(open(sys.argv[1],encoding='utf8')); new=json.loads(sys.argv[2])
 same=all(old.get(k)==new.get(k) for k in ('id','size','updated_at'))
 print('same' if same else 'different')
 raise SystemExit(0 if same else 1)
except (OSError,ValueError,KeyError): raise SystemExit(1)
PY
 then
  echo "已是最新版本：CMCC.Docker.zip（asset id $(python3 -c 'import json,sys; print(json.loads(sys.argv[1])["id"])' "$META")），不下载、不重建、不重启"
  exit 0
 fi
 URL="$(python3 -c 'import json,sys; print(json.loads(sys.argv[1])["url"])' "$META")"
 [[ -n "$URL" ]] || exit 1; echo "下载：$URL"; curl --http1.1 -fL --retry 5 --retry-all-errors --retry-delay 2 --connect-timeout 30 --speed-time 60 --speed-limit 1024 --progress-bar --show-error -o "$ARCHIVE" "$URL"
fi
[[ -s "$ARCHIVE" ]] || { echo '安装包为空'; exit 1; }
echo '[3/7] 校验并解压'; unzip -tq "$ARCHIVE"; mkdir -p "$TMP/unpack"; unzip -q "$ARCHIVE" -d "$TMP/unpack"; DF="$(find "$TMP/unpack" -type f -name novnc-Dockerfile -print -quit)"; [[ -n "$DF" ]] || exit 1; SRC="$(dirname "$DF")"; [[ -f "$SRC/novnc-compose.yml" ]] || exit 1
echo '[4/7] 停止旧容器并清理旧镜像（保留 data/.env）'; mkdir -p "$APP_DIR/data"; [[ -f "$APP_DIR/novnc-compose.yml" ]] && docker compose -f "$APP_DIR/novnc-compose.yml" down --remove-orphans --rmi local || true; [[ -f "$APP_DIR/docker-compose.yml" ]] && docker compose -f "$APP_DIR/docker-compose.yml" down --remove-orphans --rmi local || true; cp -a "$APP_DIR/.env" "$TMP/.env" 2>/dev/null || true; cp -a "$APP_DIR/data/webui-auth.json" "$TMP/webui-auth.json" 2>/dev/null || true; find "$APP_DIR" -mindepth 1 -maxdepth 1 ! -name data -exec rm -rf {} +; cp -a "$SRC"/. "$APP_DIR"/; cp -a "$TMP/.env" "$APP_DIR/.env" 2>/dev/null || true; cp -a "$TMP/webui-auth.json" "$APP_DIR/data/webui-auth.json" 2>/dev/null || true; if [[ ! -f "$APP_DIR/.env" ]]; then printf 'CMCC_WEBUI_USER=admin\nCMCC_WEBUI_PASSWORD=admin\n' > "$APP_DIR/.env"; fi; chmod 600 "$APP_DIR/.env" 2>/dev/null || true
[[ "$PORT" == 8080 ]] || sed -i "s/\"8080:8080\"/\"${PORT}:8080\"/" "$APP_DIR/novnc-compose.yml"
echo '[5/7] 检查配置'; cd "$APP_DIR"; docker compose -f novnc-compose.yml config >/dev/null
echo '[6/7] 构建镜像'; docker compose -f novnc-compose.yml build
echo '[7/7] 启动服务'; docker compose -f novnc-compose.yml up -d; sleep 5; docker compose -f novnc-compose.yml ps
if [[ -n "${META:-}" ]]; then printf '%s\n' "$META" > "$APP_DIR/.cmcc-release.json"; chmod 600 "$APP_DIR/.cmcc-release.json"; fi
echo '更新完成'
