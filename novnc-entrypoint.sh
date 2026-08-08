#!/bin/bash
set -eu
mkdir -p /data /data/profiles
chown -R cmcc:cmcc /data
mkdir -p /home/cmcc/.config
# Remove stale X11 lock/socket files left by an unclean container/client exit.
# Without this, Xvfb exits immediately with 'Server is already active', and
# every fallback slot later fails with CDP page target unavailable.
for d in 99 100 101 102 103 104 105; do
  if ! kill -0 "$(cat /tmp/.X${d}-lock 2>/dev/null)" 2>/dev/null; then
    rm -f "/tmp/.X${d}-lock" "/tmp/.X11-unix/X${d}"
  fi
done
Xvfb :100 -screen 0 1280x800x24 -ac +extension GLX +render -noreset >"$HOME/xvfb-100.log" 2>&1 &
XVFB100=$!
Xvfb :101 -screen 0 1280x800x24 -ac +extension GLX +render -noreset >"$HOME/xvfb-101.log" 2>&1 &
XVFB101=$!
Xvfb :102 -screen 0 1280x800x24 -ac +extension GLX +render -noreset >"$HOME/xvfb-102.log" 2>&1 &
XVFB102=$!
Xvfb :103 -screen 0 1280x800x24 -ac +extension GLX +render -noreset >"$HOME/xvfb-103.log" 2>&1 &
XVFB103=$!
Xvfb :104 -screen 0 1280x800x24 -ac +extension GLX +render -noreset >"$HOME/xvfb-104.log" 2>&1 &
XVFB104=$!
Xvfb :105 -screen 0 1280x800x24 -ac +extension GLX +render -noreset >"$HOME/xvfb-105.log" 2>&1 &
XVFB105=$!
Xvfb "$DISPLAY" -screen 0 1280x800x24 -ac +extension GLX +render -noreset >"$HOME/xvfb.log" 2>&1 &
XVFB=$!
openbox --display "$DISPLAY" >"$HOME/openbox.log" 2>&1 &
OPENBOX=$!
# Disable XDamage: Electron/Xvfb can otherwise leave noVNC with a blank or stale framebuffer.
x11vnc -display "$DISPLAY" -forever -shared -nopw -noxdamage -repeat -rfbport 5900 >"$HOME/x11vnc.log" 2>&1 &
X11VNC=$!
# Each heavy-client slot has its own VNC server. Websockify's token file keeps
# one public 6080 endpoint while routing live pages to the account's slot.
cat > /data/vnc.tokens <<'EOF'
slot0:127.0.0.1:5901
slot1:127.0.0.1:5902
slot2:127.0.0.1:5903
slot3:127.0.0.1:5904
slot4:127.0.0.1:5905
slot5:127.0.0.1:5906
EOF
for display_port in '100 5901' '101 5902' '102 5903' '103 5904' '104 5905' '105 5906'; do
  set -- $display_port; d=":$1"; p="$2"
  for retry in 1 2 3 4 5; do
    xdpyinfo -display "$d" >/dev/null 2>&1 && break
    sleep 1
  done
  x11vnc -display "$d" -forever -shared -nopw -noxdamage -repeat -rfbport "$p" >"$HOME/x11vnc-${p}.log" 2>&1 &
done
websockify --web=/usr/share/novnc --token-plugin=TokenFile --token-source=/data/vnc.tokens 6080 >"$HOME/novnc.log" 2>&1 &
NOVNC=$!
trap 'kill "$NOVNC" "$X11VNC" "$OPENBOX" "$XVFB" "$XVFB100" "$XVFB101" "$XVFB102" "$XVFB103" "$XVFB104" "$XVFB105" 2>/dev/null || true' EXIT
sleep 2
unset LD_LIBRARY_PATH GST_PLUGIN_PATH GST_PLUGIN_PATH_1_0 CY_BIN_PATH CY_CCSDK_PATH
exec su -s /bin/bash cmcc -c 'exec env -u LD_LIBRARY_PATH -u GST_PLUGIN_PATH -u GST_PLUGIN_PATH_1_0 python3 -m uvicorn service:app --host 0.0.0.0 --port 8080'
