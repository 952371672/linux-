#!/bin/bash
set -eu
mkdir -p /data /data/profiles
chown -R cmcc:cmcc /data
# Fixed six-slot compatibility mode: resident Xvfb/x11vnc avoids startup
# latency and matches the earlier six-slot deployment.
for n in 0 1 2 3 4 5; do
  display=$((100+n)); vnc=$((5901+n))
  Xvfb ":$display" -screen 0 1280x800x24 -ac +extension GLX +render -noreset >"$HOME/xvfb-$n.log" 2>&1 &
  for i in $(seq 1 20); do xdpyinfo -display ":$display" >/dev/null 2>&1 && break; sleep .2; done
  x11vnc -display ":$display" -forever -shared -nopw -noxdamage -repeat -rfbport "$vnc" >"$HOME/x11vnc-$n.log" 2>&1 &
done
cat > /data/vnc.tokens <<'EOF'
slot0:127.0.0.1:5901
slot1:127.0.0.1:5902
slot2:127.0.0.1:5903
slot3:127.0.0.1:5904
slot4:127.0.0.1:5905
slot5:127.0.0.1:5906
EOF
websockify --web=/usr/share/novnc --token-plugin=TokenFile --token-source=/data/vnc.tokens 6080 >"$HOME/novnc.log" 2>&1 &
NOVNC=$!
trap 'kill "$NOVNC" 2>/dev/null || true; pkill -TERM Xvfb 2>/dev/null || true; pkill -TERM x11vnc 2>/dev/null || true' EXIT
sleep 2
unset LD_LIBRARY_PATH GST_PLUGIN_PATH GST_PLUGIN_PATH_1_0 CY_BIN_PATH CY_CCSDK_PATH
exec su -s /bin/bash cmcc -c 'exec env -u LD_LIBRARY_PATH -u GST_PLUGIN_PATH -u GST_PLUGIN_PATH_1_0 python3 -m uvicorn service:app --host 0.0.0.0 --port 8080'
