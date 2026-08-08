#!/bin/bash
set -eu
mkdir -p /data /data/profiles
chown -R cmcc:cmcc /data
mkdir -p /home/cmcc/.config
Xvfb "$DISPLAY" -screen 0 1280x800x24 -ac +extension GLX +render -noreset >"$HOME/xvfb.log" 2>&1 &
XVFB=$!
openbox >"$HOME/openbox.log" 2>&1 &
OPENBOX=$!
trap 'kill "$OPENBOX" "$XVFB" 2>/dev/null || true' EXIT
sleep 2
unset LD_LIBRARY_PATH GST_PLUGIN_PATH GST_PLUGIN_PATH_1_0 CY_BIN_PATH CY_CCSDK_PATH
exec su -s /bin/bash cmcc -c 'env -u LD_LIBRARY_PATH -u GST_PLUGIN_PATH -u GST_PLUGIN_PATH_1_0 python3 -m uvicorn service:app --host 0.0.0.0 --port 8080'
