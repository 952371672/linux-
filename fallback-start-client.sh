#!/bin/bash
set -eu
BASE=/opt/chuanyun-vdi-client
ADDON="$BASE/resources/app.asar.unpacked/node_modules"
SDK="$ADDON/chuanyunAddOn/ccsdk/uos"
export DISPLAY="${DISPLAY:-:99}"
export XDG_SESSION_TYPE=x11
export XDG_CURRENT_DESKTOP=Deepin
export DESKTOP_SESSION=deepin
export XDG_SESSION_DESKTOP=deepin
export LANG="${LANG:-zh_CN.UTF-8}"
export LC_ALL="${LC_ALL:-zh_CN.UTF-8}"
export CY_BIN_PATH="$SDK/bin"
export GST_PLUGIN_PATH="$SDK/lib"
export GST_PLUGIN_PATH_1_0="$SDK/lib"
# Do not prepend the vendor SDK's bundled lib directory here. It contains
# compatibility copies such as libm.so.6 that can override Ubuntu's glibc
# and make Electron fail before opening a window. Keep only the curated native
# runtime links and netdetect libraries.
export LD_LIBRARY_PATH="/opt/cmcc-runtime-lib:$ADDON/netdetectAddOn/ntsdk/lib:${LD_LIBRARY_PATH:-}"
exec "$BASE/cmcc-jtydn" "$@"
