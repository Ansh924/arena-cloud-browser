FROM python:3.12-slim-bookworm

RUN apt-get update && apt-get install -y --no-install-recommends \
      xvfb x11vnc novnc websockify procps curl \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir playwright==1.49.0 \
    && playwright install --with-deps chromium

WORKDIR /app
COPY bridge_server.py run_browser.py start.sh ./
RUN chmod +x start.sh && mkdir -p /data/profile

ENV DISPLAY=:99 \
    PROFILE_DIR=/data/profile \
    BRIDGE=http://127.0.0.1:9080 \
    PORT=9080

EXPOSE 9080 6080
CMD ["bash", "start.sh"]
