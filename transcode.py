#!/usr/bin/env python3
"""
On-demand transcoding to a browser-playable MP4.
================================================

Archived videos in a codec no browser decodes (in practice H.265/HEVC —
see ``video_codec.py``) can be re-encoded to H.264 + AAC on request. This
is deliberately *never* automatic: a 1080p HEVC clip is a minute or more
of CPU on a NAS and roughly doubles the disk the file occupies, so it only
runs when someone clicks the button on the fallback card.

Results are cached next to the original, under ``.transcoded/`` inside the
thread's ``images`` directory, so a file is only ever converted once. The
leading dot keeps the cache out of the scraper's own globs and out of the
contact sheet.

Jobs are tracked in a module-level registry and run on worker threads with
a hard concurrency cap, so a page full of unplayable files cannot be used
to fork a dozen encoders at once.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import threading
import time
from pathlib import Path

import video_codec

log = logging.getLogger("4chan_gui.transcode")

CACHE_DIRNAME = ".transcoded"
OUTPUT_SUFFIX = ".h264.mp4"

# One encoder at a time. hordweard has other jobs to do and libx264 will
# happily eat every core it is given.
MAX_CONCURRENT = 1

# Give up on a single file rather than leaving a wedged ffmpeg forever.
JOB_TIMEOUT = 60 * 60

_jobs: dict = {}
_jobs_lock = threading.Lock()
_slots = threading.Semaphore(MAX_CONCURRENT)

_ffmpeg_path = None
_ffmpeg_checked = False


# ── ffmpeg discovery ──────────────────────────────────────────────────────────

def ffmpeg_path():
    """Path to ffmpeg, or None when the image was built without it."""
    global _ffmpeg_path, _ffmpeg_checked
    if not _ffmpeg_checked:
        _ffmpeg_path = (os.environ.get("FFMPEG_BIN")
                        or shutil.which("ffmpeg"))
        _ffmpeg_checked = True
        if _ffmpeg_path:
            log.info("ffmpeg found at %s — on-demand transcoding enabled",
                     _ffmpeg_path)
        else:
            log.info("ffmpeg not found — on-demand transcoding disabled")
    return _ffmpeg_path


def available() -> bool:
    return ffmpeg_path() is not None


# ── Cache paths ───────────────────────────────────────────────────────────────

def output_path(src: Path) -> Path:
    """Where the browser-playable copy of ``src`` lives."""
    src = Path(src)
    return src.parent / CACHE_DIRNAME / (src.name + OUTPUT_SUFFIX)


def cached(src: Path):
    """The finished output for ``src``, or None.

    A cached file older than its source is treated as stale and ignored,
    which covers the case of a thread being re-scraped over the top.
    """
    out = output_path(src)
    try:
        if not out.is_file() or out.stat().st_size == 0:
            return None
        if out.stat().st_mtime < Path(src).stat().st_mtime:
            return None
    except OSError:
        return None
    return out


# ── Job registry ──────────────────────────────────────────────────────────────

def _job_key(src: Path) -> str:
    return str(Path(src).resolve())


def status(src: Path) -> dict:
    """Current state for ``src``: done / running / queued / error / idle."""
    src = Path(src)
    if cached(src):
        return {"state": "done", "pct": 100}
    with _jobs_lock:
        job = _jobs.get(_job_key(src))
        if job:
            return dict(job)
    return {"state": "idle", "pct": 0}


def _set(key: str, **fields):
    with _jobs_lock:
        job = _jobs.setdefault(key, {"state": "queued", "pct": 0})
        job.update(fields)


# ── Encoding ──────────────────────────────────────────────────────────────────

def _build_cmd(src: Path, dest: Path) -> list:
    return [
        ffmpeg_path(),
        "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
        "-i", str(src),
        "-map", "0:v:0", "-map", "0:a:0?",
        # yuv420p is the only chroma layout every browser decoder accepts,
        # and HEVC phone exports are often 10-bit or 4:2:2.
        "-c:v", "libx264", "-pix_fmt", "yuv420p",
        "-profile:v", "high", "-level", "4.1",
        "-crf", "23", "-preset", "veryfast",
        "-c:a", "aac", "-b:a", "128k", "-ac", "2",
        # Without faststart the moov atom lands at the end of the file and
        # the browser has to range-seek before it can start playing. The
        # HEVC originals have exactly that problem.
        "-movflags", "+faststart",
        "-progress", "pipe:1", "-nostats",
        # The output is written to a ".part" file and renamed on success, so
        # the muxer has to be named explicitly — ffmpeg cannot guess it from
        # that extension and bails with "Error opening output files".
        "-f", "mp4",
        str(dest),
    ]


def _run_job(src: Path, key: str):
    dest = output_path(src)
    part = dest.with_suffix(dest.suffix + ".part")
    acquired = False
    try:
        _set(key, state="queued", pct=0, msg="waiting for an encoder slot")
        _slots.acquire()
        acquired = True
        if cached(src):                       # someone else got there first
            _set(key, state="done", pct=100, msg=None)
            return

        dest.parent.mkdir(parents=True, exist_ok=True)
        part.unlink(missing_ok=True)

        duration = (video_codec.probe(src) or {}).get("duration")
        _set(key, state="running", pct=0, msg="encoding")
        log.info("transcoding %s (%.0fs) -> H.264",
                 src.name, duration or 0)
        started = time.time()

        proc = subprocess.Popen(
            _build_cmd(src, part),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, bufsize=1,
        )
        try:
            for line in proc.stdout:
                line = line.strip()
                if line.startswith("out_time_us=") and duration:
                    try:
                        secs = int(line.split("=", 1)[1]) / 1_000_000
                    except ValueError:
                        continue
                    pct = max(0, min(99, int(secs / duration * 100)))
                    _set(key, state="running", pct=pct, msg="encoding")
                elif line == "progress=end":
                    _set(key, state="running", pct=99, msg="finalizing")
                if time.time() - started > JOB_TIMEOUT:
                    proc.kill()
                    raise TimeoutError("transcode exceeded the time limit")
            rc = proc.wait(timeout=60)
        finally:
            if proc.poll() is None:
                proc.kill()
        err = (proc.stderr.read() or "").strip() if proc.stderr else ""

        if rc != 0 or not part.is_file() or part.stat().st_size == 0:
            raise RuntimeError(err.splitlines()[-1] if err
                               else f"ffmpeg exited {rc}")

        part.replace(dest)
        log.info("transcoded %s in %.0fs (%.1f MiB -> %.1f MiB)",
                 src.name, time.time() - started,
                 src.stat().st_size / 1048576, dest.stat().st_size / 1048576)
        _set(key, state="done", pct=100, msg=None)

    except Exception as exc:                  # noqa: BLE001 - surfaced to the UI
        log.warning("transcode failed for %s: %s", Path(src).name, exc)
        try:
            part.unlink(missing_ok=True)
        except OSError:
            pass
        _set(key, state="error", pct=0, msg=str(exc)[:300])
    finally:
        if acquired:
            _slots.release()


def start(src: Path) -> dict:
    """Queue ``src`` for transcoding; returns the resulting status.

    Idempotent — calling it for a file that is already done, queued or
    running just reports that state back.
    """
    src = Path(src)
    if not available():
        return {"state": "unavailable", "pct": 0,
                "msg": "this build has no ffmpeg"}
    if cached(src):
        return {"state": "done", "pct": 100}
    key = _job_key(src)
    with _jobs_lock:
        job = _jobs.get(key)
        if job and job.get("state") in ("queued", "running"):
            return dict(job)
        _jobs[key] = {"state": "queued", "pct": 0,
                      "msg": "waiting for an encoder slot"}
    threading.Thread(target=_run_job, args=(src, key), daemon=True,
                     name=f"transcode-{src.name[:40]}").start()
    with _jobs_lock:
        return dict(_jobs[key])
