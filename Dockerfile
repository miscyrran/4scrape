# ── 4chan Archiver ─────────────────────────────────────────────────────────────
# Builds a single image containing both the web GUI (web_gui.py) and the
# board-level scraper (4chan_scraper.py).
#
# All persistent data is written to /data inside the container.
# Mount a host directory or a named volume there to keep archives across
# container restarts.
#
# Build:
#   docker build -t 4scrape .
#
# Run (with a bind mount so files are accessible on the host):
#   docker run -d \
#     -p 5000:5000 \
#     -v "$(pwd)/data:/data" \
#     --name archiver \
#     4scrape
# ──────────────────────────────────────────────────────────────────────────────

FROM python:3.11-slim

# ── Python deps ───────────────────────────────────────────────────────────────
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# ── App source ────────────────────────────────────────────────────────────────
COPY 4chan_scraper.py web_gui.py metadata_detector.py ./

# ── Data volume ───────────────────────────────────────────────────────────────
# Everything the app writes (archives, config, thread list) lives here.
# Structure inside /data:
#   /data/config.json            — scraper settings
#   /data/monitored_threads.json — GUI thread list
#   /data/archive/               — scraped text, JSON, and images
VOLUME ["/data"]

# ── Environment — all overridable at runtime ──────────────────────────────────
ENV CONFIG_PATH=/data/config.json \
    THREADS_PATH=/data/monitored_threads.json \
    ARCHIVE_DIR=/data/archive \
    HOST=0.0.0.0 \
    PORT=5000

EXPOSE 5000

# ── Entrypoint ────────────────────────────────────────────────────────────────
# web_gui.py includes the background scraper loop, so one process handles both.
# Pass --no-scheduler to run just the GUI (e.g. when using a separate
# cron/scheduler container running 4chan_scraper.py directly).
CMD ["python", "web_gui.py"]
