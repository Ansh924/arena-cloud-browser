#!/bin/bash
# Starts: virtual screen -> VNC + noVNC (admin login view) -> bridge API -> cloud browser
set -e
mkdir -p /data/profile

Xvfb :99 -screen 0 1280x900x24 &
sleep 2

VNC_PASS=$VNC_PASSWORD
if [ -z "$VNC_PASS" ]; then VNC_PASS=change-me-please; fi
x11vnc -display :99 -forever -shared -rfbport 5900 -passwd "$VNC_PASS" &
websockify --web /usr/share/novnc 6080 localhost:5900 &

python3 bridge_server.py &
sleep 1

exec python3 run_browser.py
