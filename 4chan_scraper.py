#!/usr/bin/env python3
"""
4chan Monitor & Scraper
=======================
Monitors boards and threads on a configurable schedule, saving post text
and full-sized images.  Uses the official 4chan JSON API — no HTML scraping.

Usage:
    python 4chan_scraper.py                     # uses config.json in same dir
    python 4chan_scraper.py --config myconf.json
    python 4chan_scraper.py --run-once          # single pass, no scheduler

Dependencies (pip install):
    requests schedule

4chan API rate-limit guidance: max 1 req/sec, max 1 catalog req/10 sec.
This script enforces those limits automatically.
"""

import argparse
import html
import json
import logging
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

import requests
import schedule

# ---------------------------------------------------------------------------
# Default configuration — override via config.json
# ---------------------------------------------------------------------------
DEFAULT_CONFIG = {
    # Boards to monitor (without slashes, e.g. "g", "sci", "pol")
    "boards": ["g"],

    # Only save threads whose subject OR opening post contains ANY of these
    # strings (case-insensitive).  Empty list = save every thread.
    "keywords": [],

    # Where to write archived data.
    # Override with ARCHIVE_DIR env var (e.g. /data/archive inside Docker).
    "output_dir": os.environ.get("ARCHIVE_DIR", "4chan_archive"),

    # How often to run a full scrape cycle (minutes)
    "interval_minutes": 30,

    # Max threads to scrape per board per cycle (0 = unlimited)
    "max_threads_per_board": 50,

    # Download full-size images?
    "save_images": True,

    # Skip threads whose image count is above this (0 = no limit).
    # Useful to avoid accidentally downloading massive threads.
    "max_images_per_thread": 500,

    # Download files from external links (catbox, litterbox, etc.)?
    "save_external_files": False,

    # Domains to scan for external file links
    "external_domains": ["catbox.moe", "files.catbox.moe", "litter.catbox.moe"],

    # Max external files to download per thread (0 = unlimited)
    "max_external_files_per_thread": 100,

    # Seconds to wait between API calls (4chan asks for >= 1 s)
    "request_delay": 1.0,

    # Seconds to wait between catalog fetches (4chan asks for >= 10 s)
    "catalog_delay": 10.0,

    # Save raw JSON alongside human-readable text?
    "save_raw_json": True,

    # Log level: DEBUG / INFO / WARNING / ERROR
    "log_level": "INFO",
}

# ---------------------------------------------------------------------------
# 4chan API / CDN endpoints
# ---------------------------------------------------------------------------
API_BASE = "https://a.4cdn.org"
IMG_BASE = "https://i.4cdn.org"
HEADERS  = {
    "User-Agent": "4scrape/1.0 (educational; contact via config)",
    "Accept":     "application/json",
}

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("4chan_scraper")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def load_config(path: Optional[str]) -> dict:
    cfg = dict(DEFAULT_CONFIG)
    if path and Path(path).exists():
        with open(path, "r", encoding="utf-8") as f:
            user_cfg = json.load(f)
        cfg.update(user_cfg)
        log.info("Loaded config from %s", path)
    else:
        log.info("Using default config (no config.json found)")
    return cfg


def load_state(state_path: Path) -> dict:
    """Load seen-post tracking state from disk."""
    if state_path.exists():
        with open(state_path, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}   # { board: { thread_no: last_seen_post_no } }


def save_state(state: dict, state_path: Path) -> None:
    with open(state_path, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)


def clean_html(text: str) -> str:
    """Strip 4chan HTML tags and decode entities from post comment."""
    if not text:
        return ""
    # Replace <br> / <br/> with newlines
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.IGNORECASE)
    # Remove all other tags
    text = re.sub(r"<[^>]+>", "", text)
    # Decode HTML entities (&amp; &gt; &#039; etc.)
    text = html.unescape(text)
    return text.strip()


def extract_external_links(post_html: str, allowed_domains: list) -> list:
    """
    Extract external file URLs from post HTML.

    Args:
        post_html: Raw HTML from post["com"]
        allowed_domains: List of domain names to match

    Returns:
        List of URLs found
    """
    if not post_html or not allowed_domains:
        return []

    # 4chan inserts <wbr> mid-URL for line-breaking; remove before matching
    post_html = post_html.replace("<wbr>", "")

    links = []
    for domain in allowed_domains:
        # Escape domain for regex
        domain_pattern = re.escape(domain)

        # Pattern 1: Match URLs in href attributes
        # Example: href="https://files.catbox.moe/abc123.png"
        pattern1 = rf'href="((?:https?:)?//(?:[^/]*\.)?{domain_pattern}/[^"]+)"'

        # Pattern 2: Match plain text URLs (not in attributes)
        # Example: https://files.catbox.moe/abc123.png or catbox.moe/abc123.png
        # Match until whitespace, <, or quote
        pattern2 = rf'(?:https?:)?//(?:[^/]*\.)?{domain_pattern}/[^\s<>"]+(?:\?[^\s<>"]*)?'

        # Find href links
        for match in re.findall(pattern1, post_html, re.IGNORECASE):
            if match.startswith('//'):
                match = 'https:' + match
            links.append(match)

        # Find plain text links
        for match in re.findall(pattern2, post_html, re.IGNORECASE):
            if match.startswith('//'):
                match = 'https:' + match
            elif not match.startswith('http'):
                match = 'https://' + match
            links.append(match)

    return links


def slugify(text: str, maxlen: int = 60) -> str:
    """Turn a thread subject into a safe directory-name fragment."""
    text = re.sub(r"[^\w\s-]", "", text or "")
    text = re.sub(r"[\s_-]+", "_", text).strip("_")
    return text[:maxlen]


def api_get(url: str, delay: float, retries: int = 3) -> Optional[dict]:
    """GET a 4chan API URL, respecting rate limits and retrying on 429/5xx."""
    for attempt in range(retries):
        try:
            resp = requests.get(url, headers=HEADERS, timeout=15)
            if resp.status_code == 200:
                return resp.json()
            elif resp.status_code == 404:
                log.debug("404 — thread/board gone: %s", url)
                return None
            elif resp.status_code == 429:
                wait = 30 * (attempt + 1)
                log.warning("Rate-limited (429). Waiting %d s …", wait)
                time.sleep(wait)
            else:
                log.warning("HTTP %d for %s (attempt %d)", resp.status_code, url, attempt + 1)
                time.sleep(delay * 3)
        except requests.RequestException as exc:
            log.warning("Request error (%s). Attempt %d/%d", exc, attempt + 1, retries)
            time.sleep(delay * 3)
    return None


def img_get(url: str, dest: Path, delay: float) -> bool:
    """Download an image to dest, skipping if already present."""
    if dest.exists():
        log.debug("Image already saved: %s", dest.name)
        return False
    try:
        resp = requests.get(url, headers=HEADERS, timeout=60, stream=True)
        if resp.status_code == 200:
            dest.parent.mkdir(parents=True, exist_ok=True)
            with open(dest, "wb") as f:
                for chunk in resp.iter_content(chunk_size=65536):
                    f.write(chunk)
            log.debug("Saved image: %s", dest.name)
            time.sleep(delay)
            return True
        else:
            log.warning("Image download failed HTTP %d: %s", resp.status_code, url)
    except requests.RequestException as exc:
        log.warning("Image download error (%s): %s", exc, url)
    return False


# ---------------------------------------------------------------------------
# Core scraping logic
# ---------------------------------------------------------------------------

def matches_keywords(thread: dict, keywords: list[str]) -> bool:
    """Return True if thread contains any keyword (or keyword list is empty)."""
    if not keywords:
        return True
    sub  = (thread.get("sub") or "").lower()
    body = clean_html(thread.get("com") or "").lower()
    return any(kw.lower() in sub or kw.lower() in body for kw in keywords)


def format_post(post: dict) -> str:
    """Render a single post as plain text."""
    lines = []
    ts = datetime.utcfromtimestamp(post.get("time", 0)).strftime("%Y-%m-%d %H:%M:%S UTC")
    no   = post.get("no", "?")
    name = post.get("name", "Anonymous")
    trip = post.get("trip", "")
    sub  = post.get("sub", "")

    header = f"Post #{no}  |  {name}{trip}  |  {ts}"
    if sub:
        header += f"  |  Subject: {sub}"
    lines.append(header)
    lines.append("-" * len(header))

    body = clean_html(post.get("com") or "")
    if body:
        lines.append(body)

    if post.get("filename"):
        lines.append(f"[Image: {post['filename']}{post.get('ext','')}  "
                     f"{post.get('w','?')}x{post.get('h','?')}  "
                     f"{post.get('fsize',0)//1024} KB]")
    lines.append("")
    return "\n".join(lines)


def scrape_thread(board: str, thread_no: int, cfg: dict,
                  board_dir: Path, state: dict) -> None:
    """Fetch and archive a single thread."""
    url  = f"{API_BASE}/{board}/thread/{thread_no}.json"
    data = api_get(url, cfg["request_delay"])
    if not data:
        return

    posts = data.get("posts", [])
    if not posts:
        return

    op         = posts[0]
    subject    = op.get("sub") or f"thread_{thread_no}"
    slug       = slugify(subject)
    thread_dir = board_dir / f"{thread_no}_{slug}"
    thread_dir.mkdir(parents=True, exist_ok=True)

    # Determine which posts we haven't seen yet
    board_state   = state.setdefault(board, {})
    last_seen     = board_state.get(str(thread_no), 0)
    new_posts     = [p for p in posts if p["no"] > last_seen]

    if not new_posts:
        log.debug("/%s/ thread %d — no new posts", board, thread_no)
        return

    log.info("/%s/ thread %d — %d new post(s)  [%s]", board, thread_no, len(new_posts), slug[:40])

    # ---- Save raw JSON ----
    if cfg["save_raw_json"]:
        json_path = thread_dir / "thread.json"
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    # ---- Append plain-text posts ----
    txt_path = thread_dir / "posts.txt"
    mode = "a" if txt_path.exists() else "w"
    with open(txt_path, mode, encoding="utf-8") as f:
        if mode == "w":
            # Write thread header on first pass
            f.write(f"Board: /{board}/\n")
            f.write(f"Thread: {thread_no}\n")
            f.write(f"Subject: {clean_html(op.get('sub') or '(no subject)')}\n")
            f.write(f"Archived: {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}\n")
            f.write("=" * 70 + "\n\n")
        for post in new_posts:
            f.write(format_post(post))
            f.write("\n")

    # ---- Download images ----
    if cfg["save_images"]:
        img_dir       = thread_dir / "images"
        images_in_thread = [p for p in posts if p.get("tim") and p.get("ext")]
        new_images    = [p for p in new_posts if p.get("tim") and p.get("ext")]

        max_imgs = cfg.get("max_images_per_thread", 0)
        if max_imgs and len(images_in_thread) > max_imgs:
            log.info("  Skipping images — thread has %d images (limit %d)",
                     len(images_in_thread), max_imgs)
        else:
            img_dir.mkdir(parents=True, exist_ok=True)
            for post in new_images:
                tim  = post["tim"]
                ext  = post["ext"]
                orig = html.unescape(post.get("filename", str(tim)))
                # Use original filename; fall back to tim-based name
                dest_name = f"{orig}{ext}"
                # Sanitise filename
                dest_name = re.sub(r'[<>:"/\\|?*]', "_", dest_name)
                dest = img_dir / dest_name
                img_url = f"{IMG_BASE}/{board}/{tim}{ext}"
                img_get(img_url, dest, cfg["request_delay"])

    # ---- Download external files ----
    if cfg.get("save_external_files", False):
        external_domains = cfg.get("external_domains", [])
        max_external = cfg.get("max_external_files_per_thread", 0)

        if external_domains:
            # Extract all external links from posts
            external_links = []
            for post in posts:
                if post.get("com"):  # Post has text content
                    links = extract_external_links(post["com"], external_domains)
                    for link in links:
                        external_links.append((link, post["no"]))

            # Apply limit
            if max_external > 0:
                external_links = external_links[:max_external]

            # Download each external file
            if external_links:
                img_dir = thread_dir / "images"
                img_dir.mkdir(parents=True, exist_ok=True)

                # Track URLs we've already seen to avoid duplicates
                seen_urls = set()
                for idx, (url, post_no) in enumerate(external_links, start=1):
                    if url in seen_urls:
                        continue
                    seen_urls.add(url)

                    # Extract filename from URL
                    filename = url.split("/")[-1].split("?")[0]
                    if not filename:
                        filename = f"download{idx}"

                    # Prepend post number and index
                    dest_name = f"{post_no}_{idx}_{filename}"
                    # Sanitize filename
                    dest_name = re.sub(r'[<>:"/\\|?*]', "_", dest_name)
                    dest_path = img_dir / dest_name

                    img_get(url, dest_path, cfg["request_delay"])

    # Update state
    board_state[str(thread_no)] = posts[-1]["no"]


def scrape_board(board: str, cfg: dict, archive_dir: Path, state: dict) -> None:
    """Fetch catalog for a board and scrape matching threads."""
    log.info("Fetching catalog for /%s/ …", board)
    url  = f"{API_BASE}/{board}/catalog.json"
    data = api_get(url, cfg["catalog_delay"])
    if not data:
        log.warning("Could not fetch catalog for /%s/", board)
        return

    time.sleep(cfg["catalog_delay"])

    # Catalog is a list of pages; each page has a "threads" list
    threads = []
    for page in data:
        threads.extend(page.get("threads", []))

    keywords = cfg.get("keywords", [])
    matched  = [t for t in threads if matches_keywords(t, keywords)]

    max_t = cfg.get("max_threads_per_board", 0)
    if max_t:
        matched = matched[:max_t]

    log.info("/%s/  %d threads on board, %d match filter", board, len(threads), len(matched))

    board_dir = archive_dir / board
    board_dir.mkdir(parents=True, exist_ok=True)

    for i, thread in enumerate(matched, 1):
        thread_no = thread["no"]
        log.debug("  [%d/%d] thread %d", i, len(matched), thread_no)
        scrape_thread(board, thread_no, cfg, board_dir, state)
        time.sleep(cfg["request_delay"])


# ---------------------------------------------------------------------------
# Scheduler entry point
# ---------------------------------------------------------------------------

def run_cycle(cfg: dict, archive_dir: Path, state_path: Path) -> None:
    """One full scrape cycle across all configured boards."""
    log.info("=" * 60)
    log.info("Starting scrape cycle  %s", datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC"))
    log.info("=" * 60)

    state = load_state(state_path)

    for board in cfg["boards"]:
        try:
            scrape_board(board, cfg, archive_dir, state)
        except Exception as exc:
            log.error("Error scraping /%s/: %s", board, exc, exc_info=True)

    save_state(state, state_path)
    log.info("Cycle complete. State saved.")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Monitor and archive 4chan boards on a schedule."
    )
    parser.add_argument(
        "--config",
        default=os.environ.get("CONFIG_PATH", "config.json"),
        help="Path to JSON config file (env: CONFIG_PATH, default: config.json)"
    )
    parser.add_argument(
        "--run-once", action="store_true",
        help="Run a single scrape cycle and exit (no scheduler)"
    )
    args = parser.parse_args()

    cfg         = load_config(args.config)
    log.setLevel(getattr(logging, cfg.get("log_level", "INFO").upper(), logging.INFO))

    archive_dir = Path(cfg["output_dir"])
    archive_dir.mkdir(parents=True, exist_ok=True)
    state_path  = archive_dir / "_state.json"

    log.info("Output directory : %s", archive_dir.resolve())
    log.info("Boards           : %s", ", ".join(f"/{b}/" for b in cfg["boards"]))
    log.info("Keywords         : %s", cfg["keywords"] or "(all threads)")
    log.info("Interval         : %d min", cfg["interval_minutes"])
    log.info("Save images      : %s", cfg["save_images"])

    if args.run_once:
        run_cycle(cfg, archive_dir, state_path)
        return

    # Schedule recurring runs
    interval = cfg["interval_minutes"]
    schedule.every(interval).minutes.do(run_cycle, cfg, archive_dir, state_path)

    log.info("Scheduler started — running every %d minutes. Press Ctrl+C to stop.", interval)

    # Run immediately on start, then on schedule
    run_cycle(cfg, archive_dir, state_path)

    while True:
        schedule.run_pending()
        time.sleep(10)


if __name__ == "__main__":
    main()
