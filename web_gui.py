#!/usr/bin/env python3
"""
4chan Archiver — Web GUI
========================
Flask-based web interface for managing and monitoring 4chan thread archiving.
Designed to work alongside (or replace) 4chan_scraper.py.

Requirements:
    pip install flask requests schedule

Usage:
    python web_gui.py              # http://localhost:5000
    python web_gui.py --port 8080
    python web_gui.py --no-scheduler   # GUI only, no auto-scraping
"""

import argparse
import html as html_lib
import json
import logging
import os
import re
import sys
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import requests
import schedule
from flask import Flask, jsonify, request

# ── Logging ───────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("4chan_gui")

# ── Constants ─────────────────────────────────────────────────────────────────

API_BASE     = "https://a.4cdn.org"
IMG_BASE     = "https://i.4cdn.org"
HTTP_HEADERS = {"User-Agent": "4scrape/1.0", "Accept": "application/json"}

# Paths — override with environment variables so the Docker volume
# (/data) becomes the single location for all persistent state.
CONFIG_PATH  = Path(os.environ.get("CONFIG_PATH",  "config.json"))
THREADS_PATH = Path(os.environ.get("THREADS_PATH", "monitored_threads.json"))

DEFAULT_CONFIG = {
    # ARCHIVE_DIR env var sets where scraped content is written.
    # Inside Docker this is /data/archive; outside it is 4chan_archive/.
    "output_dir":            os.environ.get("ARCHIVE_DIR", "4chan_archive"),
    "interval_minutes":      30,
    "save_images":           True,
    "max_images_per_thread": 200,
    "save_raw_json":         True,
    "request_delay":         1.0,
    "log_level":             "INFO",
    "follow_new_threads":    True,
    "follow_near_bump_limit": True,
    "follow_cross_board":    False,
    "follow_tag_auto_added": True,
    "follow_keywords":       ["new thread", "new bread", "bake", "baked"],
    "auto_archive_on_404":          True,
    "auto_archive_on_4chan_archive": True,
}

# ── Config I/O ────────────────────────────────────────────────────────────────

def load_cfg() -> dict:
    cfg = dict(DEFAULT_CONFIG)
    if CONFIG_PATH.exists():
        with open(CONFIG_PATH, encoding="utf-8") as f:
            cfg.update({k: v for k, v in json.load(f).items()
                        if not k.startswith("_")})
    return cfg

def save_cfg(cfg: dict):
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump({k: v for k, v in cfg.items() if not k.startswith("_")},
                  f, indent=2)

# ── Thread list I/O ───────────────────────────────────────────────────────────

_threads_lock = threading.Lock()

def load_threads() -> list:
    if THREADS_PATH.exists():
        with open(THREADS_PATH, encoding="utf-8") as f:
            return json.load(f)
    return []

def save_threads(threads: list):
    with open(THREADS_PATH, "w", encoding="utf-8") as f:
        json.dump(threads, f, indent=2, default=str)

# ── URL parsing ───────────────────────────────────────────────────────────────

def parse_4chan_url(url: str) -> Optional[tuple]:
    """Return (board, thread_no) from any 4chan thread URL, or None."""
    url = url.strip().rstrip("/")
    for pat in [
        r'boards\.4chan(?:nel)?\.org/([a-zA-Z0-9]+)/thread/(\d+)',
        r'4chan(?:nel)?\.org/([a-zA-Z0-9]+)/thread/(\d+)',
    ]:
        m = re.search(pat, url, re.IGNORECASE)
        if m:
            return m.group(1).lower(), int(m.group(2))
    return None

# ── Scraping utilities (adapted from 4chan_scraper.py) ────────────────────────

def _api_get(url: str, retries: int = 3) -> Optional[dict]:
    for attempt in range(retries):
        try:
            r = requests.get(url, headers=HTTP_HEADERS, timeout=15)
            if r.status_code == 200:
                return r.json()
            if r.status_code == 404:
                return None
            if r.status_code == 429:
                time.sleep(30 * (attempt + 1))
            else:
                time.sleep(3)
        except requests.RequestException as exc:
            log.warning("API error: %s", exc)
            time.sleep(3)
    return None

def _clean_html(text: str) -> str:
    if not text:
        return ""
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", "", text)
    return html_lib.unescape(text).strip()

_CROSSTHREAD_RE = re.compile(
    r'href="(?:(?:https?:)?//boards\.4chan(?:nel)?\.org)?/([a-z0-9]+)/thread/(\d+)',
    re.IGNORECASE,
)

def _find_successor_threads(new_posts: list, src_board: str, cfg: dict) -> list:
    """Return deduplicated [(board, thread_no), ...] for posts that contain
    both a follow-keyword and a cross-thread link. Returns [] if disabled."""
    if not cfg.get("follow_new_threads", True):
        return []
    keywords = [kw.lower() for kw in cfg.get("follow_keywords", []) if kw.strip()]
    if not keywords:
        return []
    allow_cross = cfg.get("follow_cross_board", False)
    found = {}
    for post in new_posts:
        raw_html = post.get("com") or ""
        links = _CROSSTHREAD_RE.findall(raw_html)
        if not links:
            continue
        plain = _clean_html(raw_html).lower()
        if not any(kw in plain for kw in keywords):
            continue
        for board, thread_no_str in links:
            board = board.lower()
            if not allow_cross and board != src_board:
                continue
            found[(board, int(thread_no_str))] = True
    return list(found.keys())

def _slugify(text: str, maxlen: int = 60) -> str:
    text = re.sub(r"[^\w\s-]", "", text or "")
    text = re.sub(r"[\s_-]+", "_", text).strip("_")
    return text[:maxlen]

def _format_post(post: dict) -> str:
    ts   = datetime.utcfromtimestamp(post.get("time", 0)).strftime("%Y-%m-%d %H:%M:%S UTC")
    no   = post.get("no", "?")
    name = post.get("name", "Anonymous")
    trip = post.get("trip", "")
    sub  = post.get("sub", "")
    hdr  = f"Post #{no}  |  {name}{trip}  |  {ts}"
    if sub:
        hdr += f"  |  Subject: {sub}"
    lines = [hdr, "-" * len(hdr)]
    body = _clean_html(post.get("com") or "")
    if body:
        lines.append(body)
    if post.get("filename"):
        lines.append(f"[Image: {post['filename']}{post.get('ext','')}  "
                     f"{post.get('w','?')}x{post.get('h','?')}]")
    lines.append("")
    return "\n".join(lines)

def scrape_thread_entry(t: dict, cfg: dict) -> tuple:
    """Fetch and archive one thread. Returns (updated_thread_dict, discovered_list).

    discovered_list is [(board, thread_no), ...] of successor threads found via
    follow-keywords. Empty list when the feature is disabled or no matches found.
    """
    board     = t["board"]
    thread_no = t["thread_no"]
    delay     = cfg.get("request_delay", 1.0)
    archive   = Path(cfg.get("output_dir", "4chan_archive"))

    data = _api_get(f"{API_BASE}/{board}/thread/{thread_no}.json")
    if data is None:
        t["status"] = "404"
        if cfg.get("auto_archive_on_404", True):
            t["user_archived"] = True
        return t, []

    posts = data.get("posts", [])
    if not posts:
        return t, []

    op    = posts[0]
    title = _clean_html(op.get("sub") or "")
    slug  = _slugify(title or f"thread_{thread_no}")

    board_dir = archive / board
    board_dir.mkdir(parents=True, exist_ok=True)

    existing   = list(board_dir.glob(f"{thread_no}_*"))
    thread_dir = existing[0] if existing else board_dir / f"{thread_no}_{slug}"
    thread_dir.mkdir(parents=True, exist_ok=True)

    last_seen = t.get("last_seen_post", 0)
    new_posts = [p for p in posts if p["no"] > last_seen]

    discovered = []
    if new_posts:
        # Raw JSON
        if cfg.get("save_raw_json", True):
            with open(thread_dir / "thread.json", "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)

        # Plain text
        txt      = thread_dir / "posts.txt"
        is_new   = not txt.exists() or txt.stat().st_size == 0
        with open(txt, "a", encoding="utf-8") as f:
            if is_new:
                f.write(f"Board: /{board}/\nThread: {thread_no}\n")
                f.write(f"Subject: {title or '(no subject)'}\n")
                f.write("=" * 70 + "\n\n")
            for p in new_posts:
                f.write(_format_post(p) + "\n")

        # Images
        if cfg.get("save_images", True):
            max_i = cfg.get("max_images_per_thread", 0)
            all_i = [p for p in posts if p.get("tim") and p.get("ext")]
            if not max_i or len(all_i) <= max_i:
                img_dir = thread_dir / "images"
                img_dir.mkdir(exist_ok=True)
                for p in [x for x in new_posts if x.get("tim") and x.get("ext")]:
                    fname = re.sub(r'[<>:"/\\|?*]',
                                   "_", f"{p.get('filename', p['tim'])}{p['ext']}")
                    dest = img_dir / fname
                    if not dest.exists():
                        try:
                            r = requests.get(
                                f"{IMG_BASE}/{board}/{p['tim']}{p['ext']}",
                                headers=HTTP_HEADERS, timeout=60, stream=True)
                            if r.status_code == 200:
                                with open(dest, "wb") as f:
                                    for chunk in r.iter_content(65536):
                                        f.write(chunk)
                        except Exception as exc:
                            log.warning("Image DL error: %s", exc)
                        time.sleep(delay)

        t["last_seen_post"] = posts[-1]["no"]

        near_bump = cfg.get("follow_near_bump_limit", True)
        if not near_bump or len(posts) >= 300:
            discovered = _find_successor_threads(new_posts, board, cfg)

    # Update stats
    t["title"]        = title or t.get("title") or f"Thread {thread_no}"
    t["post_count"]   = len(posts)
    t["last_scraped"] = datetime.utcnow().isoformat() + "Z"
    t["status"]       = "archived" if op.get("archived") else "active"
    if t["status"] == "archived" and cfg.get("auto_archive_on_4chan_archive", True):
        t["user_archived"] = True

    img_dir = thread_dir / "images"
    t["image_count"] = (
        len([f for f in img_dir.iterdir() if f.is_file()])
        if img_dir.exists() else 0
    )
    return t, discovered

# ── Scheduler / background runner ─────────────────────────────────────────────

_run_lock  = threading.Lock()
_run_state = {"running": False, "next_run_ts": None}

def run_all_threads():
    acquired = _run_lock.acquire(blocking=False)
    if not acquired:
        log.info("Scrape cycle already running — skipping")
        return
    _run_state["running"] = True
    try:
        cfg = load_cfg()
        log.info("── Scrape cycle starting ──")
        with _threads_lock:
            threads = load_threads()

        updated        = []
        all_discovered = []
        for t in threads:
            if t.get("status") == "404" or t.get("user_archived"):
                updated.append(t)
                continue
            log.info("  /%s/ thread %d", t["board"], t["thread_no"])
            try:
                t, discovered = scrape_thread_entry(t, cfg)
                all_discovered.extend(discovered)
            except Exception as exc:
                log.error("  Error: %s", exc, exc_info=True)
            updated.append(t)
            time.sleep(cfg.get("request_delay", 1.0))

        with _threads_lock:
            save_threads(updated)

        for board, thread_no in all_discovered:
            try:
                _auto_add_thread(board, thread_no, cfg)
            except Exception as exc:
                log.error("Auto-follow error /%s/%d: %s", board, thread_no, exc)

        interval = cfg.get("interval_minutes", 30)
        _run_state["next_run_ts"] = (
            datetime.utcnow() + timedelta(minutes=interval)
        ).isoformat() + "Z"
        log.info("── Cycle complete. Next run at %s ──", _run_state["next_run_ts"])
    finally:
        _run_state["running"] = False
        _run_lock.release()

def _auto_add_thread(board: str, thread_no: int, cfg: dict):
    """Add a discovered successor thread to the monitored list if not already present.
    Must NOT be called while holding _threads_lock — acquires the lock itself.
    """
    tid = f"{board}_{thread_no}"
    with _threads_lock:
        ts = load_threads()
        if any(t["id"] == tid for t in ts):
            log.debug("Auto-follow skip (already monitored): %s", tid)
            return
        new_t = {
            "id":             tid,
            "board":          board,
            "thread_no":      thread_no,
            "url":            f"https://boards.4chan.org/{board}/thread/{thread_no}",
            "title":          f"Thread {thread_no}",
            "post_count":     0,
            "image_count":    0,
            "last_scraped":   None,
            "last_seen_post": 0,
            "status":         "pending",
            "added_at":       datetime.utcnow().isoformat() + "Z",
        }
        if cfg.get("follow_tag_auto_added", True):
            new_t["auto_added"] = True
        ts.append(new_t)
        save_threads(ts)
        log.info("Auto-followed new thread: /%s/ %d", board, thread_no)

    def _initial_scrape():
        c = load_cfg()
        with _threads_lock:
            current = load_threads()
        idx = next((i for i, t in enumerate(current) if t["id"] == tid), None)
        if idx is not None:
            current[idx], _ = scrape_thread_entry(current[idx], c)
            with _threads_lock:
                save_threads(current)

    threading.Thread(target=_initial_scrape, daemon=True,
                     name=f"auto-scrape-{tid}").start()

def start_scheduler(interval: int):
    _run_state["next_run_ts"] = (
        datetime.utcnow() + timedelta(minutes=interval)
    ).isoformat() + "Z"
    schedule.every(interval).minutes.do(run_all_threads)

    def loop():
        while True:
            schedule.run_pending()
            time.sleep(10)

    threading.Thread(target=loop, daemon=True, name="scheduler").start()
    log.info("Scheduler running every %d min", interval)

# ── Flask app ─────────────────────────────────────────────────────────────────

app = Flask(__name__)
app.json.sort_keys = False

@app.route("/")
def index():
    return HTML_TEMPLATE, 200, {"Content-Type": "text/html; charset=utf-8"}

@app.route("/api/threads", methods=["GET"])
def api_get_threads():
    with _threads_lock:
        threads = load_threads()
    return jsonify(threads)

@app.route("/api/threads", methods=["POST"])
def api_add_thread():
    data = request.get_json(silent=True) or {}
    url  = (data.get("url") or "").strip()

    parsed = parse_4chan_url(url)
    if not parsed:
        return jsonify({"error": "Not a valid 4chan thread URL"}), 400

    board, thread_no = parsed
    tid = f"{board}_{thread_no}"

    with _threads_lock:
        threads = load_threads()
        if any(t["id"] == tid for t in threads):
            return jsonify({"error": "Thread is already being monitored"}), 409

        new_t = {
            "id":             tid,
            "board":          board,
            "thread_no":      thread_no,
            "url":            f"https://boards.4chan.org/{board}/thread/{thread_no}",
            "title":          f"Thread {thread_no}",
            "post_count":     0,
            "image_count":    0,
            "last_scraped":   None,
            "last_seen_post": 0,
            "status":         "pending",
            "added_at":       datetime.utcnow().isoformat() + "Z",
        }
        threads.append(new_t)
        save_threads(threads)

    # Kick off an immediate scrape in the background
    def _initial_scrape():
        cfg = load_cfg()
        with _threads_lock:
            ts = load_threads()
        idx = next((i for i, t in enumerate(ts) if t["id"] == tid), None)
        if idx is not None:
            ts[idx], _ = scrape_thread_entry(ts[idx], cfg)
            with _threads_lock:
                save_threads(ts)

    threading.Thread(target=_initial_scrape, daemon=True).start()
    return jsonify(new_t), 201

@app.route("/api/threads/<tid>", methods=["DELETE"])
def api_remove_thread(tid: str):
    with _threads_lock:
        threads = load_threads()
        before  = len(threads)
        threads = [t for t in threads if t["id"] != tid]
        if len(threads) == before:
            return jsonify({"error": "Thread not found"}), 404
        save_threads(threads)
    return jsonify({"ok": True})

@app.route("/api/threads/<tid>", methods=["PATCH"])
def api_update_thread(tid: str):
    data = request.get_json(silent=True) or {}
    with _threads_lock:
        threads = load_threads()
        idx = next((i for i, t in enumerate(threads) if t["id"] == tid), None)
        if idx is None:
            return jsonify({"error": "Thread not found"}), 404
        if "user_archived" in data:
            threads[idx]["user_archived"] = bool(data["user_archived"])
        save_threads(threads)
    return jsonify(threads[idx])

@app.route("/api/threads/<tid>/scrape", methods=["POST"])
def api_scrape_one(tid: str):
    cfg = load_cfg()

    def _run():
        with _threads_lock:
            ts = load_threads()
        idx = next((i for i, t in enumerate(ts) if t["id"] == tid), None)
        if idx is not None:
            ts[idx], _ = scrape_thread_entry(ts[idx], cfg)
            with _threads_lock:
                save_threads(ts)

    threading.Thread(target=_run, daemon=True).start()
    return jsonify({"ok": True})

@app.route("/api/status", methods=["GET"])
def api_status():
    cfg = load_cfg()
    return jsonify({
        "running":          _run_state["running"],
        "next_run_ts":      _run_state["next_run_ts"],
        "interval_minutes": cfg.get("interval_minutes", 30),
    })

@app.route("/api/config", methods=["GET"])
def api_get_config():
    return jsonify(load_cfg())

@app.route("/api/config", methods=["POST"])
def api_set_config():
    data = request.get_json(silent=True) or {}
    cfg  = load_cfg()
    allowed = (
        "interval_minutes", "save_images", "max_images_per_thread",
        "save_raw_json", "request_delay", "output_dir",
        "follow_new_threads", "follow_near_bump_limit", "follow_cross_board",
        "follow_tag_auto_added", "follow_keywords",
        "auto_archive_on_404", "auto_archive_on_4chan_archive",
    )
    for key in allowed:
        if key in data:
            cfg[key] = data[key]
    save_cfg(cfg)
    # Reschedule with new interval
    schedule.clear()
    interval = int(cfg.get("interval_minutes", 30))
    start_scheduler(interval)
    return jsonify(cfg)

@app.route("/api/debug/follow/<board>/<int:thread_no>")
def api_debug_follow(board: str, thread_no: int):
    """Diagnostic: fetch a thread and report what thread-following would detect.
    Ignores last_seen_post and bump-limit — checks every post."""
    cfg      = load_cfg()
    keywords = [kw.lower() for kw in cfg.get("follow_keywords", []) if kw.strip()]
    allow_cross = cfg.get("follow_cross_board", False)

    data = _api_get(f"{API_BASE}/{board}/thread/{thread_no}.json")
    if data is None:
        return jsonify({"error": "Thread not found or 404"}), 404

    posts = data.get("posts", [])
    results = []
    for p in posts:
        raw = p.get("com") or ""
        links  = _CROSSTHREAD_RE.findall(raw)
        plain  = _clean_html(raw).lower()
        kw_hits = [kw for kw in keywords if kw in plain]
        filtered_links = [
            {"board": b.lower(), "thread_no": int(n)}
            for b, n in links
            if allow_cross or b.lower() == board
        ]
        would_follow = bool(kw_hits and filtered_links)
        if kw_hits or links:
            results.append({
                "post_no":       p["no"],
                "keywords_found": kw_hits,
                "links_found":   [{"board": b, "thread_no": int(n)} for b, n in links],
                "links_allowed": filtered_links,
                "would_follow":  would_follow,
                "raw_html":      raw,
            })

    return jsonify({
        "board":           board,
        "thread_no":       thread_no,
        "total_posts":     len(posts),
        "posts_with_hits": len(results),
        "follow_enabled":  cfg.get("follow_new_threads", True),
        "keywords":        keywords,
        "allow_cross_board": allow_cross,
        "results":         results,
    })

@app.route("/api/run", methods=["POST"])
def api_run_now():
    if _run_state["running"]:
        return jsonify({"error": "A scrape cycle is already running"}), 409
    threading.Thread(target=run_all_threads, daemon=True).start()
    return jsonify({"ok": True})

@app.route("/archive/<board>/<int:thread_no>")
def archive_view(board: str, thread_no: int):
    """Render the locally-archived version of a thread."""
    cfg        = load_cfg()
    archive    = Path(cfg.get("output_dir", "4chan_archive"))
    board_dir  = archive / board
    matches    = list(board_dir.glob(f"{thread_no}_*")) if board_dir.exists() else []
    thread_dir = matches[0] if matches else None

    # Try thread.json first (richest data), fall back to posts.txt
    thread_json = thread_dir / "thread.json" if thread_dir else None
    posts_txt   = thread_dir / "posts.txt"   if thread_dir else None

    live_url = f"https://boards.4chan.org/{board}/thread/{thread_no}"

    if thread_json and thread_json.exists():
        with open(thread_json, encoding="utf-8") as f:
            data = json.load(f)
        posts = data.get("posts", [])
        op    = posts[0] if posts else {}
        title = html_lib.unescape(re.sub(r"<[^>]+>", "", op.get("sub") or "")) or f"Thread {thread_no}"

        def render_post(p):
            no   = p.get("no", "?")
            name = html_lib.escape(p.get("name") or "Anonymous")
            trip = html_lib.escape(p.get("trip") or "")
            ts   = datetime.utcfromtimestamp(p.get("time", 0)).strftime("%Y-%m-%d %H:%M:%S UTC")
            sub  = html_lib.unescape(re.sub(r"<[^>]+>", "", p.get("sub") or ""))
            com = p.get("com") or ""
            # Preserve quotelinks before stripping all other tags
            com = re.sub(r'<a[^>]*class="quotelink"[^>]*>&gt;&gt;(\d+)</a>',
                         '\x00QL\\1\x00', com)
            com = re.sub(r"<br\s*/?>", "\n", com, flags=re.IGNORECASE)
            com = re.sub(r"<[^>]+>", "", com)
            com = html_lib.unescape(com).strip()

            img_html = ""
            if p.get("tim") and p.get("ext") and thread_dir:
                orig = re.sub(r'[<>:"/\\|?*]', "_",
                              f"{p.get('filename', p['tim'])}{p['ext']}")
                img_path = thread_dir / "images" / orig
                ext_lower = (p.get("ext") or "").lower()
                if img_path.exists():
                    src_url = f"/archive-img/{board}/{thread_no}/{html_lib.escape(orig)}"
                    if ext_lower in (".webm", ".mp4"):
                        img_html = (
                            f'<div class="post-img">'
                            f'<video controls preload="metadata" '
                            f'src="{src_url}">'
                            f'<a href="{src_url}" target="_blank">{html_lib.escape(orig)}</a>'
                            f'</video></div>'
                        )
                    else:
                        img_html = (
                            f'<div class="post-img">'
                            f'<img src="{src_url}" '
                            f'alt="{html_lib.escape(orig)}" loading="lazy"></div>'
                        )

            sub_html = f'<div class="post-sub">{html_lib.escape(sub)}</div>' if sub else ""
            if com:
                com_safe = html_lib.escape(com)
                com_safe = re.sub(r'\x00QL(\d+)\x00',
                                  r'<a href="#p\1" class="quotelink">&gt;&gt;\1</a>',
                                  com_safe)
                com_html = f'<pre class="post-body">{com_safe}</pre>'
            else:
                com_html = ""
            return (
                f'<div class="post" id="p{no}">'
                f'<div class="post-hdr">'
                f'<span class="post-name">{name}{trip}</span>'
                f'<span class="post-no">#{no}</span>'
                f'<span class="post-ts">{ts}</span>'
                f'</div>'
                f'{sub_html}{img_html}{com_html}'
                f'</div>'
            )

        posts_html = "\n".join(render_post(p) for p in posts)
        body = f'<div class="posts">{posts_html}</div>'
    elif posts_txt and posts_txt.exists():
        with open(posts_txt, encoding="utf-8") as f:
            raw = html_lib.escape(f.read())
        title = f"Thread {thread_no}"
        body  = f'<pre class="raw-txt">{raw}</pre>'
    else:
        title = f"Thread {thread_no}"
        body  = ('<div class="not-found">'
                 '<h2>Not yet archived</h2>'
                 f'<p>No local archive found for /{board}/{thread_no}.</p>'
                 f'<p><a href="{live_url}" target="_blank" rel="noopener">View on 4chan &#8599;</a></p>'
                 '</div>')

    page = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html_lib.escape(title)} — 4scrape archive</title>
<style>
*,*::before,*::after{{box-sizing:border-box;margin:0;padding:0}}
:root{{--bg:#0c0c0e;--surface:#131316;--surface2:#1a1a1f;--border:#2c2c34;
       --text:#d8d8e0;--muted:#6b6b7a;--accent:#c93535;--green:#22c55e;
       --blue:#60a5fa;--orange:#f59e0b}}
body{{background:var(--bg);color:var(--text);
      font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;
      font-size:14px;line-height:1.6;min-height:100vh}}
header{{background:var(--surface);border-bottom:1px solid var(--border);
        padding:.75rem 1.5rem;display:flex;align-items:center;gap:1rem;
        position:sticky;top:0;z-index:100}}
.logo{{font-size:.95rem;font-weight:800;letter-spacing:.12em;color:var(--accent)}}
.hdr-title{{font-size:.9rem;color:var(--text);overflow:hidden;
             text-overflow:ellipsis;white-space:nowrap;max-width:600px}}
.spacer{{flex:1}}
.live-link{{font-size:.8rem;color:var(--muted);text-decoration:none;
            border:1px solid var(--border);border-radius:5px;padding:.3rem .7rem;
            white-space:nowrap;transition:all .15s}}
.live-link:hover{{background:var(--surface2);color:var(--text)}}
.back-link{{font-size:.8rem;color:var(--muted);text-decoration:none;
             border:1px solid var(--border);border-radius:5px;padding:.3rem .7rem;
             white-space:nowrap;transition:all .15s}}
.back-link:hover{{background:var(--surface2);color:var(--text)}}
main{{max-width:860px;margin:0 auto;padding:1.4rem 1.2rem}}
.thread-info{{background:var(--surface);border:1px solid var(--border);
               border-radius:8px;padding:.9rem 1.1rem;margin-bottom:1.2rem;
               font-size:.82rem;color:var(--muted)}}
.thread-info strong{{color:var(--text)}}
.posts{{display:flex;flex-direction:column;gap:.85rem}}
.post{{background:var(--surface);border:1px solid var(--border);
        border-radius:8px;padding:.9rem 1rem;overflow:hidden}}
.post-hdr{{display:flex;align-items:baseline;gap:.7rem;
            margin-bottom:.55rem;flex-wrap:wrap}}
.post-name{{font-weight:600;font-size:.82rem;color:var(--green)}}
.post-no{{font-size:.78rem;color:var(--muted);font-family:'Courier New',monospace}}
.post-ts{{font-size:.75rem;color:var(--muted);margin-left:auto}}
.post-sub{{font-weight:700;font-size:.93rem;color:var(--text);margin-bottom:.45rem}}
.post-body{{white-space:pre-wrap;word-break:break-word;font-family:inherit;
             font-size:.87rem;color:var(--text)}}
.post-img{{margin:.5rem 0}}
.post-img img{{max-width:min(100%,400px);max-height:320px;
                object-fit:contain;border-radius:5px;
                border:1px solid var(--border);cursor:pointer}}
.post-img img.expanded{{max-width:100%;max-height:none}}
.post-img video{{max-width:min(100%,560px);max-height:400px;
                  border-radius:5px;border:1px solid var(--border);
                  display:block;background:#000}}
.raw-txt{{white-space:pre-wrap;word-break:break-word;font-family:'Courier New',monospace;
           font-size:.8rem;background:var(--surface);border:1px solid var(--border);
           border-radius:8px;padding:1rem}}
.quotelink{{color:var(--blue);text-decoration:none}}
.quotelink:hover{{text-decoration:underline}}
.not-found{{text-align:center;padding:4rem 1rem;color:var(--muted)}}
.not-found h2{{color:var(--text);margin-bottom:.75rem}}
.not-found a{{color:var(--blue)}}
</style>
</head>
<body>
<header>
  <span class="logo">4SCRAPE</span>
  <span class="hdr-title">/{html_lib.escape(board)}/ &nbsp;&#8250;&nbsp; {html_lib.escape(title)}</span>
  <div class="spacer"></div>
  <a class="back-link" href="/">&#8592; Back</a>
  <a class="live-link" href="{live_url}" target="_blank" rel="noopener">Live on 4chan &#8599;</a>
</header>
<main>
  <div class="thread-info">
    Board: <strong>/{html_lib.escape(board)}/</strong> &nbsp;·&nbsp;
    Thread: <strong>{thread_no}</strong> &nbsp;·&nbsp;
    Archived locally
  </div>
  {body}
</main>
<script>
document.querySelectorAll('.post-img img').forEach(img => {{
  img.addEventListener('click', () => img.classList.toggle('expanded'));
}});
</script>
</body>
</html>"""
    return page, 200, {"Content-Type": "text/html; charset=utf-8"}


@app.route("/archive-img/<board>/<int:thread_no>/<path:filename>")
def archive_img(board: str, thread_no: int, filename: str):
    """Serve a locally-archived image file."""
    import mimetypes
    from flask import send_file
    cfg       = load_cfg()
    archive   = Path(cfg.get("output_dir", "4chan_archive"))
    board_dir = archive / board
    matches   = list(board_dir.glob(f"{thread_no}_*")) if board_dir.exists() else []
    if not matches:
        return "Not found", 404
    # Sanitise the filename — no path traversal
    safe = Path(filename).name
    img  = matches[0] / "images" / safe
    if not img.exists() or not img.is_file():
        return "Not found", 404
    mime = mimetypes.guess_type(safe)[0] or "application/octet-stream"
    return send_file(img, mimetype=mime)


# ── HTML Template ─────────────────────────────────────────────────────────────

HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>4scrape</title>
<style>
*, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
:root {
  --bg:       #0c0c0e;
  --surface:  #131316;
  --surface2: #1a1a1f;
  --surface3: #222228;
  --border:   #2c2c34;
  --border2:  #3a3a44;
  --accent:   #c93535;
  --accent-h: #a82828;
  --text:     #d8d8e0;
  --muted:    #6b6b7a;
  --green:    #22c55e;
  --orange:   #f59e0b;
  --red:      #ef4444;
  --blue:     #60a5fa;
}
body {
  background: var(--bg);
  color: var(--text);
  font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
  font-size: 14px;
  line-height: 1.5;
  min-height: 100vh;
}

/* ── Header ── */
header {
  background: var(--surface);
  border-bottom: 1px solid var(--border);
  padding: 0 1.5rem;
  height: 52px;
  display: flex;
  align-items: center;
  gap: 1rem;
  position: sticky;
  top: 0;
  z-index: 100;
  box-shadow: 0 1px 12px rgba(0,0,0,.4);
}
.logo {
  font-size: 1rem;
  font-weight: 800;
  letter-spacing: .12em;
  text-transform: uppercase;
  color: var(--accent);
  display: flex;
  align-items: center;
  gap: .45rem;
}
.logo .sep { color: var(--muted); font-weight: 300; }
.logo .sub { color: var(--text); font-weight: 400; }
header .spacer { flex: 1; }
.run-indicator {
  font-size: .8rem;
  color: var(--muted);
  display: flex;
  align-items: center;
  gap: .4rem;
  display: none;
}

/* ── Buttons ── */
.btn {
  background: var(--surface2);
  color: var(--text);
  border: 1px solid var(--border);
  padding: .38rem .85rem;
  border-radius: 6px;
  cursor: pointer;
  font-size: .825rem;
  font-family: inherit;
  transition: background .15s, border-color .15s;
  white-space: nowrap;
}
.btn:hover { background: var(--surface3); border-color: var(--border2); }
.btn.primary {
  background: var(--accent);
  border-color: var(--accent);
  color: #fff;
  font-weight: 600;
}
.btn.primary:hover { background: var(--accent-h); border-color: var(--accent-h); }
.btn:disabled { opacity: .45; cursor: not-allowed; }
.btn-icon {
  background: transparent;
  color: var(--muted);
  border: 1px solid var(--border);
  border-radius: 5px;
  padding: .2rem .48rem;
  cursor: pointer;
  font-size: .8rem;
  font-family: inherit;
  line-height: 1.5;
  transition: all .15s;
}
.btn-icon:hover { background: var(--surface2); color: var(--text); border-color: var(--border2); }
.btn-icon.danger:hover { background: rgba(239,68,68,.1); color: var(--red); border-color: rgba(239,68,68,.5); }

/* ── Layout ── */
main { max-width: 1160px; margin: 0 auto; padding: 1.4rem 1.5rem; }

/* ── Card ── */
.card {
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: 10px;
  padding: 1.2rem 1.3rem;
  margin-bottom: 1.2rem;
}
.card-title {
  font-size: .7rem;
  font-weight: 700;
  color: var(--muted);
  text-transform: uppercase;
  letter-spacing: .12em;
  margin-bottom: .9rem;
}

/* ── Drop zone ── */
#drop-zone {
  border: 2px dashed var(--border2);
  border-radius: 8px;
  padding: .85rem 1rem;
  transition: border-color .2s, background .2s;
  cursor: text;
  background: var(--bg);
}
#drop-zone:focus-within { border-color: var(--border2); }
#drop-zone.drag-over {
  border-color: var(--accent);
  background: rgba(201,53,53,.05);
}
#url-input {
  width: 100%;
  background: transparent;
  border: none;
  outline: none;
  color: var(--text);
  font-size: .925rem;
  font-family: inherit;
  resize: none;
  min-height: 4rem;
}
#url-input::placeholder { color: var(--muted); }
.input-footer {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-top: .65rem;
}
.input-hint {
  font-size: .75rem;
  color: var(--muted);
  display: flex;
  align-items: center;
  gap: .35rem;
}
.hint-pill {
  background: var(--surface3);
  border: 1px solid var(--border);
  border-radius: 4px;
  padding: .05rem .35rem;
  font-size: .7rem;
  color: var(--muted);
}

/* ── Threads section ── */
.threads-header {
  display: flex;
  align-items: center;
  gap: .65rem;
  margin-bottom: .9rem;
}
.threads-header .card-title { margin-bottom: 0; }
.count-badge {
  background: var(--surface3);
  border: 1px solid var(--border);
  border-radius: 99px;
  padding: .1rem .5rem;
  font-size: .72rem;
  color: var(--muted);
  font-variant-numeric: tabular-nums;
}
.next-run {
  margin-left: auto;
  font-size: .78rem;
  color: var(--muted);
}
.next-run strong { color: var(--text); font-weight: 500; }

/* ── Title cell ── */
.title-cell {
  display: flex;
  align-items: center;
  gap: .4rem;
  min-width: 0;
}
.live-btn {
  flex-shrink: 0;
  font-size: .72rem;
  color: var(--muted);
  border: 1px solid var(--border);
  border-radius: 4px;
  padding: .12rem .38rem;
  text-decoration: none;
  white-space: nowrap;
  background: transparent;
  transition: all .15s;
  line-height: 1.5;
}
.live-btn:hover {
  background: var(--surface2);
  color: var(--text);
  border-color: var(--border2);
}

/* ── Table ── */
.t-wrap { overflow-x: auto; }
.thread-table {
  width: 100%;
  border-collapse: collapse;
}
.thread-table th {
  text-align: left;
  font-size: .7rem;
  font-weight: 700;
  color: var(--muted);
  text-transform: uppercase;
  letter-spacing: .1em;
  padding: .5rem .7rem;
  border-bottom: 1px solid var(--border);
  white-space: nowrap;
}
.thread-table td {
  padding: .7rem .7rem;
  border-bottom: 1px solid var(--border);
  vertical-align: middle;
}
.thread-table tbody tr:last-child td { border-bottom: none; }
.thread-table tbody tr:hover td { background: rgba(255,255,255,.022); }
.col-title { min-width: 240px; max-width: 340px; }
.title-link {
  color: var(--text);
  text-decoration: none;
  font-weight: 500;
  display: block;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  max-width: 320px;
  transition: color .15s;
}
.title-link:hover { color: var(--accent); }
.board-tag {
  display: inline-block;
  background: rgba(96,165,250,.1);
  color: var(--blue);
  border: 1px solid rgba(96,165,250,.2);
  border-radius: 5px;
  padding: .1rem .42rem;
  font-size: .78rem;
  font-family: 'Courier New', monospace;
  font-weight: 600;
  white-space: nowrap;
}
.num {
  font-variant-numeric: tabular-nums;
  font-size: .875rem;
}
.time-text { font-size: .8rem; color: var(--muted); white-space: nowrap; }
.status-cell {
  display: flex;
  align-items: center;
  gap: .4rem;
  font-size: .82rem;
  white-space: nowrap;
}
.dot {
  width: 7px; height: 7px;
  border-radius: 50%;
  flex-shrink: 0;
}
.s-active   .dot { background: var(--green); box-shadow: 0 0 5px var(--green); }
.s-archived .dot { background: var(--orange); }
.s-404      .dot { background: var(--red); }
.s-pending  .dot { background: var(--muted); animation: blink 1.2s ease-in-out infinite; }
@keyframes blink { 0%,100%{opacity:1} 50%{opacity:.25} }
.actions-cell { display: flex; gap: .35rem; }

/* ── Empty state ── */
.empty {
  text-align: center;
  padding: 3.5rem 1rem;
  color: var(--muted);
}
.empty-icon { font-size: 2rem; margin-bottom: .75rem; opacity: .5; }
.empty h3 { color: var(--text); font-size: .95rem; margin-bottom: .35rem; }
.empty p  { font-size: .83rem; }

/* ── Settings ── */
details > summary {
  cursor: pointer;
  list-style: none;
  font-size: .82rem;
  color: var(--muted);
  user-select: none;
  display: flex;
  align-items: center;
  gap: .4rem;
}
details > summary::before { content: "▸"; transition: transform .2s; display: inline-block; }
details[open] > summary::before { transform: rotate(90deg); }
.settings-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(200px, 1fr));
  gap: .85rem;
  margin-top: 1rem;
}
.setting label {
  display: block;
  font-size: .72rem;
  font-weight: 600;
  color: var(--muted);
  text-transform: uppercase;
  letter-spacing: .08em;
  margin-bottom: .3rem;
}
.setting input[type="number"],
.setting input[type="text"] {
  width: 100%;
  background: var(--surface2);
  border: 1px solid var(--border);
  border-radius: 5px;
  color: var(--text);
  padding: .32rem .6rem;
  font-size: .875rem;
  font-family: inherit;
  transition: border-color .15s;
}
.setting input[type="number"]:focus,
.setting input[type="text"]:focus {
  outline: none;
  border-color: var(--accent);
}
.setting.checkbox-row {
  display: flex;
  align-items: center;
  gap: .5rem;
  padding-top: 1.2rem;
}
.setting.checkbox-row label {
  margin-bottom: 0;
  font-size: .825rem;
  text-transform: none;
  letter-spacing: 0;
  color: var(--text);
  cursor: pointer;
}
.setting input[type="checkbox"] {
  accent-color: var(--accent);
  width: .95rem; height: .95rem;
  cursor: pointer;
}
.settings-footer {
  margin-top: .9rem;
  display: flex;
  gap: .5rem;
  align-items: center;
}
.save-ok { font-size: .8rem; color: var(--green); display: none; }

/* ── Toasts ── */
#toasts {
  position: fixed;
  bottom: 1.4rem;
  right: 1.4rem;
  z-index: 9999;
  display: flex;
  flex-direction: column;
  gap: .5rem;
  pointer-events: none;
}
.toast {
  background: var(--surface2);
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: .7rem 1rem;
  font-size: .84rem;
  max-width: 340px;
  pointer-events: auto;
  animation: toast-in .2s ease;
  box-shadow: 0 4px 20px rgba(0,0,0,.4);
}
.toast.success { border-left: 3px solid var(--green); }
.toast.error   { border-left: 3px solid var(--red); }
.toast.info    { border-left: 3px solid var(--blue); }
@keyframes toast-in { from { transform: translateX(16px); opacity: 0; } }

/* ── Spinner ── */
.spinner {
  display: inline-block;
  width: 11px; height: 11px;
  border: 2px solid var(--border2);
  border-top-color: var(--accent);
  border-radius: 50%;
  animation: spin .55s linear infinite;
}
@keyframes spin { to { transform: rotate(360deg); } }
.auto-tag { color: var(--orange); font-size: .72rem; cursor: default;
            flex-shrink: 0; user-select: none; }
.archive-summary {{ display: flex; align-items: center; gap: .65rem;
                    cursor: pointer; list-style: none; padding: .1rem 0; }}
.archive-summary::before {{ content: "▸"; transition: transform .2s;
                             display: inline-block; color: var(--muted); }}
details[open] > .archive-summary::before {{ transform: rotate(90deg); }}
.archive-summary .card-title {{ margin-bottom: 0; }}
</style>
</head>
<body>

<header>
  <div class="logo">
    4SCRAPE
  </div>
  <div class="spacer"></div>
  <div class="run-indicator" id="run-indicator">
    <span class="spinner"></span> Scraping…
  </div>
  <button class="btn primary" id="run-btn" onclick="runAll()">&#9654; Run Now</button>
</header>

<main>

  <!-- Add Thread -->
  <div class="card">
    <div class="card-title">Add Thread</div>
    <div id="drop-zone"
         ondragover="onDragOver(event)"
         ondragleave="onDragLeave(event)"
         ondrop="onDrop(event)">
      <textarea id="url-input"
                placeholder="Paste or drag a 4chan thread URL here&#10;e.g. https://boards.4chan.org/g/thread/12345678"
                rows="2"
                onkeydown="if(event.key==='Enter'&&!event.shiftKey){event.preventDefault();addThread()}"
                oninput="clearErr()"></textarea>
    </div>
    <div class="input-footer">
      <span class="input-hint">
        <span class="hint-pill">Enter</span> to add &nbsp;·&nbsp;
        <span class="hint-pill">Ctrl+V</span> paste &nbsp;·&nbsp;
        drag tab from browser
      </span>
      <button class="btn primary" id="add-btn" onclick="addThread()">+ Add Thread</button>
    </div>
    <div id="url-error" style="margin-top:.5rem;font-size:.8rem;color:var(--red);display:none"></div>
  </div>

  <!-- Thread List -->
  <div class="card">
    <div class="threads-header">
      <div class="card-title">Monitored Threads</div>
      <span class="count-badge" id="count-badge">0</span>
      <div class="next-run">
        Next run: <strong id="next-run-display">—</strong>
      </div>
    </div>
    <div id="thread-list"></div>
  </div>

  <!-- Archived Threads -->
  <div class="card">
    <details id="archive-details">
      <summary class="archive-summary">
        <div class="card-title">Archived Threads</div>
        <span class="count-badge" id="archive-count-badge">0</span>
      </summary>
      <div id="archive-list" style="margin-top:.9rem"></div>
    </details>
  </div>

  <!-- Settings -->
  <div class="card">
    <details id="settings-details">
      <summary>Settings</summary>
      <div class="settings-grid">
        <div class="setting">
          <label>Interval (minutes)</label>
          <input type="number" id="cfg-interval" min="5" max="1440" value="30">
        </div>
        <div class="setting">
          <label>Max images / thread</label>
          <input type="number" id="cfg-max-img" min="0" value="200">
        </div>
        <div class="setting">
          <label>Request delay (sec)</label>
          <input type="number" id="cfg-delay" min="0.5" max="30" step="0.5" value="1">
        </div>
        <div class="setting">
          <label>Output directory</label>
          <input type="text" id="cfg-output" value="4chan_archive">
        </div>
        <div class="setting checkbox-row">
          <input type="checkbox" id="cfg-images" checked>
          <label for="cfg-images">Download images</label>
        </div>
        <div class="setting checkbox-row">
          <input type="checkbox" id="cfg-json" checked>
          <label for="cfg-json">Save raw JSON</label>
        </div>
      </div>
      <details style="margin-top:1rem">
        <summary>Thread Following</summary>
        <div class="settings-grid" style="margin-top:.85rem">
          <div class="setting checkbox-row">
            <input type="checkbox" id="cfg-follow-enabled" checked>
            <label for="cfg-follow-enabled">Enable thread following</label>
          </div>
          <div class="setting checkbox-row">
            <input type="checkbox" id="cfg-follow-bump" checked>
            <label for="cfg-follow-bump">Only near bump limit (&#8805;300 posts)</label>
          </div>
          <div class="setting checkbox-row">
            <input type="checkbox" id="cfg-follow-cross">
            <label for="cfg-follow-cross">Allow cross-board links</label>
          </div>
          <div class="setting checkbox-row">
            <input type="checkbox" id="cfg-follow-tag" checked>
            <label for="cfg-follow-tag">Tag auto-added threads</label>
          </div>
          <div class="setting" style="grid-column:1/-1">
            <label>Keywords (one per line)</label>
            <textarea id="cfg-follow-keywords" rows="4"
              style="width:100%;background:var(--surface2);border:1px solid var(--border);
                     border-radius:5px;color:var(--text);padding:.32rem .6rem;
                     font-size:.875rem;font-family:inherit;resize:vertical;
                     transition:border-color .15s;outline:none"
              onfocus="this.style.borderColor='var(--accent)'"
              onblur="this.style.borderColor='var(--border)'">new thread
new bread
bake
baked</textarea>
          </div>
        </div>
      </details>
      <details style="margin-top:1rem">
        <summary>Auto-archive</summary>
        <div class="settings-grid" style="margin-top:.85rem">
          <div class="setting checkbox-row">
            <input type="checkbox" id="cfg-autoarchive-404" checked>
            <label for="cfg-autoarchive-404">Auto-archive when thread 404s</label>
          </div>
          <div class="setting checkbox-row">
            <input type="checkbox" id="cfg-autoarchive-4chan" checked>
            <label for="cfg-autoarchive-4chan">Auto-archive when 4chan archives the thread</label>
          </div>
        </div>
      </details>
      <div class="settings-footer">
        <button class="btn" onclick="saveConfig()">Save Settings</button>
        <span class="save-ok" id="save-ok">&#10003; Saved</span>
      </div>
    </details>
  </div>

</main>

<div id="toasts"></div>

<script>
// ── State ──────────────────────────────────────────────────────────────────────
let threads    = [];
let nextRunTs  = null;
let isRunning  = false;

// ── Boot ───────────────────────────────────────────────────────────────────────
document.addEventListener('DOMContentLoaded', () => {
  loadConfig();
  refresh();
  setInterval(refresh, 15000);
  setInterval(tickCountdown, 1000);

  // Global paste: if nothing editable is focused, populate URL input
  document.addEventListener('paste', e => {
    const tag = document.activeElement && document.activeElement.tagName;
    if (tag === 'INPUT' || tag === 'TEXTAREA') return;
    const txt = e.clipboardData.getData('text/plain') || '';
    if (is4chanUrl(txt.trim())) {
      document.getElementById('url-input').value = txt.trim();
      toast('URL pasted — press Enter or click Add Thread', 'info');
    }
  });
});

// ── Refresh ────────────────────────────────────────────────────────────────────
async function refresh() {
  await Promise.all([fetchThreads(), fetchStatus()]);
}

async function fetchThreads() {
  try {
    const r = await fetch('/api/threads');
    if (!r.ok) return;
    threads = await r.json();
    renderTable();
  } catch (_) {}
}

async function fetchStatus() {
  try {
    const r    = await fetch('/api/status');
    const data = await r.json();
    isRunning  = data.running;
    nextRunTs  = data.next_run_ts ? new Date(data.next_run_ts) : null;
    document.getElementById('run-indicator').style.display = isRunning ? 'flex' : 'none';
    document.getElementById('run-btn').disabled = isRunning;
  } catch (_) {}
}

async function loadConfig() {
  try {
    const r   = await fetch('/api/config');
    const cfg = await r.json();
    document.getElementById('cfg-interval').value  = cfg.interval_minutes ?? 30;
    document.getElementById('cfg-max-img').value   = cfg.max_images_per_thread ?? 200;
    document.getElementById('cfg-delay').value     = cfg.request_delay ?? 1;
    document.getElementById('cfg-output').value    = cfg.output_dir ?? '4chan_archive';
    document.getElementById('cfg-images').checked  = cfg.save_images !== false;
    document.getElementById('cfg-json').checked    = cfg.save_raw_json !== false;
    document.getElementById('cfg-follow-enabled').checked = cfg.follow_new_threads !== false;
    document.getElementById('cfg-follow-bump').checked    = cfg.follow_near_bump_limit !== false;
    document.getElementById('cfg-follow-cross').checked   = cfg.follow_cross_board === true;
    document.getElementById('cfg-follow-tag').checked     = cfg.follow_tag_auto_added !== false;
    const kws = Array.isArray(cfg.follow_keywords) ? cfg.follow_keywords : [];
    document.getElementById('cfg-follow-keywords').value  = kws.join('\\n');
    document.getElementById('cfg-autoarchive-404').checked  = cfg.auto_archive_on_404 !== false;
    document.getElementById('cfg-autoarchive-4chan').checked = cfg.auto_archive_on_4chan_archive !== false;
  } catch (_) {}
}

// ── Render table ───────────────────────────────────────────────────────────────
function makeTable(rows) {
  return `<div class="t-wrap">
    <table class="thread-table">
      <thead><tr>
        <th>Title</th><th>Board</th><th>Posts</th>
        <th>Images</th><th>Last Run</th><th>Status</th><th></th>
      </tr></thead>
      <tbody>${rows}</tbody>
    </table></div>`;
}

function makeRow(t, isArchived) {
  const sc   = 's-' + (t.status || 'pending');
  const slbl = {active:'Active', archived:'Archived', '404':'404 Gone', pending:'Pending'}[t.status] || t.status;
  const last = t.last_scraped ? timeAgo(new Date(t.last_scraped)) : '—';
  const ttl  = esc(t.title || 'Thread ' + t.thread_no);
  const actions = isArchived
    ? `<button class="btn-icon" title="Unarchive" onclick="unarchiveThread('${esc(t.id)}')">&#x21A9;</button>
       <button class="btn-icon danger" title="Remove" onclick="removeThread('${esc(t.id)}')">&#x2715;</button>`
    : `<button class="btn-icon" title="Archive" onclick="archiveThread('${esc(t.id)}')">&#x229F;</button>
       <button class="btn-icon" title="Scrape now" onclick="scrapeOne('${esc(t.id)}')">&#x21BB;</button>
       <button class="btn-icon danger" title="Remove" onclick="removeThread('${esc(t.id)}')">&#x2715;</button>`;
  return `<tr>
      <td class="col-title"><div class="title-cell">
        <a class="title-link" href="/archive/${esc(t.board)}/${esc(t.thread_no)}" target="_blank" title="${ttl}">${ttl}</a>
        ${t.auto_added ? '<span class="auto-tag" title="Auto-followed">&#x2935;</span>' : ''}
        <a class="live-btn" href="${esc(t.url)}" target="_blank" rel="noopener" title="Open on 4chan">4chan &#8599;</a>
      </div></td>
      <td><span class="board-tag">/${esc(t.board)}/</span></td>
      <td class="num">${t.post_count ?? 0}</td>
      <td class="num">${t.image_count ?? 0}</td>
      <td class="time-text">${last}</td>
      <td><div class="status-cell ${sc}"><span class="dot"></span>${slbl}</div></td>
      <td class="actions-cell">${actions}</td>
    </tr>`;
}

function renderTable() {
  const active   = threads.filter(t => !t.user_archived);
  const archived = threads.filter(t =>  t.user_archived);

  document.getElementById('count-badge').textContent         = active.length;
  document.getElementById('archive-count-badge').textContent = archived.length;

  const el = document.getElementById('thread-list');
  if (!active.length) {
    el.innerHTML = `
      <div class="empty">
        <div class="empty-icon">&#128065;</div>
        <h3>No threads yet</h3>
        <p>Add a 4chan thread URL above to start monitoring it.</p>
      </div>`;
  } else {
    el.innerHTML = makeTable(active.map(t => makeRow(t, false)).join(''));
  }

  const al = document.getElementById('archive-list');
  if (!archived.length) {
    al.innerHTML = '<div class="empty" style="padding:1.5rem 1rem"><p>No archived threads yet.</p></div>';
  } else {
    al.innerHTML = makeTable(archived.map(t => makeRow(t, true)).join(''));
  }
}

// ── Actions ────────────────────────────────────────────────────────────────
async function addThread() {
  const inp = document.getElementById('url-input');
  const btn = document.getElementById('add-btn');
  const url = inp ? inp.value.trim() : '';

  if (!url) {
    toast('Paste a 4chan thread URL first', 'error');
    if (inp) inp.focus();
    return;
  }

  if (!is4chanUrl(url)) {
    const msg = 'Not a valid 4chan thread URL — expected: boards.4chan.org/{board}/thread/{id}';
    showErr(msg);
    toast(msg, 'error');
    return;
  }

  if (btn) { btn.disabled = true; btn.textContent = 'Adding…'; }
  clearErr();

  try {
    const r = await fetch('/api/threads', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({url})
    });

    let data;
    try { data = await r.json(); }
    catch (e) { throw new Error('Server returned an unexpected response (status ' + r.status + ')'); }

    if (!r.ok) {
      const msg = (data && data.error) || ('Server error ' + r.status);
      showErr(msg);
      toast(msg, 'error');
      return;
    }

    if (inp) inp.value = '';
    clearErr();
    toast('Added /' + data.board + '/ thread ' + data.thread_no + ' — scraping now…', 'success');
    await fetchThreads();
    setTimeout(fetchThreads, 5000);
    setTimeout(fetchThreads, 12000);

  } catch (err) {
    console.error('[addThread]', err);
    const msg = err.message || 'Network error — is the server running?';
    showErr(msg);
    toast(msg, 'error');
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = '+ Add Thread'; }
  }
}

async function removeThread(id) {
  try {
    const r = await fetch('/api/threads/' + encodeURIComponent(id), {method: 'DELETE'});
    if (r.ok) { toast('Thread removed', 'info'); await fetchThreads(); }
  } catch (_) { toast('Network error', 'error'); }
}

async function archiveThread(id) {
  try {
    const r = await fetch('/api/threads/' + encodeURIComponent(id), {
      method: 'PATCH',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({user_archived: true})
    });
    if (r.ok) { await fetchThreads(); }
    else { toast('Could not archive thread', 'error'); }
  } catch (_) { toast('Network error', 'error'); }
}

async function unarchiveThread(id) {
  try {
    const r = await fetch('/api/threads/' + encodeURIComponent(id), {
      method: 'PATCH',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({user_archived: false})
    });
    if (r.ok) { await fetchThreads(); }
    else { toast('Could not unarchive thread', 'error'); }
  } catch (_) { toast('Network error', 'error'); }
}

async function scrapeOne(id) {
  try {
    const r = await fetch('/api/threads/' + encodeURIComponent(id) + '/scrape', {method: 'POST'});
    if (r.ok) {
      toast('Scraping thread…', 'info');
      setTimeout(fetchThreads, 5000);
      setTimeout(fetchThreads, 12000);
    }
  } catch (_) { toast('Network error', 'error'); }
}

async function runAll() {
  try {
    const r    = await fetch('/api/run', {method: 'POST'});
    const data = await r.json();
    if (!r.ok) { toast(data.error || 'Could not start run', 'error'); return; }
    toast('Scrape cycle started', 'info');
    await fetchStatus();
    [3000, 10000, 25000, 60000].forEach(d => setTimeout(refresh, d));
  } catch (_) { toast('Network error', 'error'); }
}

async function saveConfig() {
  const rawKws   = document.getElementById('cfg-follow-keywords').value;
  const keywords = rawKws.split('\\n').map(s => s.trim()).filter(s => s.length > 0);
  const cfg = {
    interval_minutes:       parseInt(document.getElementById('cfg-interval').value) || 30,
    max_images_per_thread:  parseInt(document.getElementById('cfg-max-img').value)  || 0,
    request_delay:          parseFloat(document.getElementById('cfg-delay').value)  || 1,
    output_dir:             document.getElementById('cfg-output').value || '4chan_archive',
    save_images:            document.getElementById('cfg-images').checked,
    save_raw_json:          document.getElementById('cfg-json').checked,
    follow_new_threads:     document.getElementById('cfg-follow-enabled').checked,
    follow_near_bump_limit: document.getElementById('cfg-follow-bump').checked,
    follow_cross_board:     document.getElementById('cfg-follow-cross').checked,
    follow_tag_auto_added:  document.getElementById('cfg-follow-tag').checked,
    follow_keywords:        keywords,
    auto_archive_on_404:          document.getElementById('cfg-autoarchive-404').checked,
    auto_archive_on_4chan_archive: document.getElementById('cfg-autoarchive-4chan').checked,
  };
  try {
    const r = await fetch('/api/config', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(cfg)
    });
    if (r.ok) {
      const ok = document.getElementById('save-ok');
      ok.style.display = 'inline';
      setTimeout(() => ok.style.display = 'none', 2500);
    } else {
      toast('Failed to save settings', 'error');
    }
  } catch (_) { toast('Network error', 'error'); }
}

// ── Drag & drop ────────────────────────────────────────────────────────────────
function onDragOver(e) {
  e.preventDefault();
  e.stopPropagation();
  e.dataTransfer.dropEffect = 'copy';
  document.getElementById('drop-zone').classList.add('drag-over');
}
function onDragLeave(e) {
  document.getElementById('drop-zone').classList.remove('drag-over');
}
function onDrop(e) {
  e.preventDefault();
  e.stopPropagation();
  document.getElementById('drop-zone').classList.remove('drag-over');

  const raw = e.dataTransfer.getData('text/uri-list')
           || e.dataTransfer.getData('text/x-moz-url')
           || e.dataTransfer.getData('text/plain')
           || '';

  const firstUrl = raw.split(/\s+/).find(l => l.trim() && !l.startsWith('#'));
  if (firstUrl) {
    document.getElementById('url-input').value = firstUrl.trim();
    clearErr();
    addThread();
  }
}

// ── Countdown ────────────────────────────────────────────────────────────────
function tickCountdown() {
  const el = document.getElementById('next-run-display');
  if (!nextRunTs) { el.textContent = '—'; return; }
  const sec = Math.max(0, Math.floor((nextRunTs - Date.now()) / 1000));
  if (sec === 0) { el.textContent = 'now'; return; }
  const m = Math.floor(sec / 60), s = sec % 60;
  el.textContent = m > 0
    ? m + 'm ' + String(s).padStart(2,'0') + 's'
    : s + 's';
}

// ── Utilities ────────────────────────────────────────────────────────────────
function is4chanUrl(s) {
  return /4chan(nel)?\.org\/[a-zA-Z0-9]+\/thread\/\d+/.test(s);
}
function timeAgo(d) {
  const sec = Math.floor((Date.now() - d) / 1000);
  if (sec < 60)    return sec + 's ago';
  if (sec < 3600)  return Math.floor(sec / 60) + 'm ago';
  if (sec < 86400) return Math.floor(sec / 3600) + 'h ago';
  return Math.floor(sec / 86400) + 'd ago';
}
function esc(s) {
  return String(s)
    .replace(/&/g,'&amp;').replace(/</g,'&lt;')
    .replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}
function toast(msg, type) {
  const c = document.getElementById('toasts');
  const el = document.createElement('div');
  el.className = 'toast ' + (type || 'info');
  el.textContent = msg;
  c.appendChild(el);
  setTimeout(() => el.remove(), 4200);
}
function showErr(msg) {
  const el = document.getElementById('url-error');
  el.textContent = msg;
  el.style.display = 'block';
}
function clearErr() {
  document.getElementById('url-error').style.display = 'none';
}
</script>
</body>
</html>"""

# ── Entry point ────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="4scrape — Web GUI")
    parser.add_argument("--host", default=os.environ.get("HOST", "127.0.0.1"),
                        help="Host to bind (env: HOST, default: 127.0.0.1; "
                             "use 0.0.0.0 inside Docker)")
    parser.add_argument("--port", type=int,
                        default=int(os.environ.get("PORT", 5000)),
                        help="Port to listen on (env: PORT, default: 5000)")
    parser.add_argument("--no-scheduler", action="store_true",
                        help="Start GUI only, no background scraping")
    args = parser.parse_args()

    cfg = load_cfg()
    log.setLevel(getattr(logging, cfg.get("log_level", "INFO").upper(), logging.INFO))

    if not args.no_scheduler:
        interval = int(cfg.get("interval_minutes", 30))
        existing = load_threads()
        if existing:
            threading.Thread(target=run_all_threads, daemon=True,
                             name="initial-scrape").start()
        start_scheduler(interval)

    log.info("Starting 4scrape at http://%s:%d", args.host, args.port)
    app.run(host=args.host, port=args.port, debug=False, use_reloader=False)


if __name__ == "__main__":
    main()
