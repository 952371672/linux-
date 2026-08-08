#!/bin/bash
set -eu
mkdir -p /data /data/profiles
chown -R cmcc:cmcc /data
# Client displays are created on demand by service.py for each heavy fallback.
# Keep only the lightweight noVNC router resident; a token becomes usable when
# its corresponding slot's Xvfb/x11vnc is created.
cat > /data/vnc.tokens <<'EOF'
slot0:127.0.0.1:5901
slot1:127.0.0.1:5902
slot2:127.0.0.1:5903
slot3:127.0.0.1:5904
slot4:127.0.0.1:5905
slot5:127.0.0.1:5906
slot6:127.0.0.1:5907
slot7:127.0.0.1:5908
EOF
websockify --web=/usr/share/novnc --token-plugin=TokenFile --token-source=/data/vnc.tokens 6080 >"$HOME/novnc.log" 2>&1 &
NOVNC=$!
trap 'kill "$NOVNC" 2>/dev/null || true' EXIT
sleep 2
unset LD_LIBRARY_PATH GST_PLUGIN_PATH GST_PLUGIN_PATH_1_0 CY_BIN_PATH CY_CCSDK_PATH
exec su -s /bin/bash cmcc -c 'exec env -u LD_LIBRARY_PATH -u GST_PLUGIN_PATH -u GST_PLUGIN_PATH_1_0 python3 -m uvicorn service:app --host 0.0.0.0 --port 8080'
