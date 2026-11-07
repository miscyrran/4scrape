#!/usr/bin/env python3
"""
Video codec sniffing — pure Python, no ffprobe required.
========================================================

Some archived videos are perfectly valid files that no browser will play.
The common case is H.265/HEVC in an MP4 container: VLC bundles its own
decoder so the file plays fine locally, but Firefox has no built-in HEVC
decoder at all and the ``<video>`` element just fails with
"no video with supported format and MIME type found".

Rather than shell out to ffprobe (which is not in the image and would mean
a subprocess per thumbnail), this module reads the container headers
directly. For MP4 that means walking the box tree to the ``stsd`` sample
description and reading the four-character sample entry format — ``avc1``
for H.264, ``hvc1``/``hev1`` for HEVC, and so on. Only a few kilobytes are
read per file, and results are memoised on (path, mtime, size).

The policy is deliberately asymmetric: a codec is only reported as
unplayable when it is positively identified as one browsers reject.
Anything unrecognized is assumed playable, so a sniffing failure shows a
normal player (which may then fail and be handled client-side) rather than
hiding a file that would have worked.
"""

from __future__ import annotations

import logging
import struct
import threading
from pathlib import Path

log = logging.getLogger("4chan_gui.video_codec")

# ── Codec tables ──────────────────────────────────────────────────────────────

# MP4 sample entry formats. Values are friendly names.
_MP4_VIDEO_CODECS = {
    b"avc1": "H.264",
    b"avc3": "H.264",
    b"hvc1": "HEVC",
    b"hev1": "HEVC",
    b"hvt1": "HEVC",
    b"dvh1": "Dolby Vision (HEVC)",
    b"dvhe": "Dolby Vision (HEVC)",
    b"vvc1": "VVC/H.266",
    b"vvi1": "VVC/H.266",
    b"av01": "AV1",
    b"vp08": "VP8",
    b"vp09": "VP9",
    b"mp4v": "MPEG-4 Part 2",
    b"s263": "H.263",
    b"jpeg": "Motion JPEG",
    b"ap4h": "ProRes",
    b"apch": "ProRes",
    b"apcn": "ProRes",
    b"apcs": "ProRes",
    b"apco": "ProRes",
}

# Matroska/WebM CodecID strings.
_MKV_VIDEO_CODECS = {
    b"V_MPEG4/ISO/AVC": "H.264",
    b"V_MPEGH/ISO/HEVC": "HEVC",
    b"V_AV1": "AV1",
    b"V_VP8": "VP8",
    b"V_VP9": "VP9",
    b"V_MPEG4/ISO/ASP": "MPEG-4 Part 2",
    b"V_MPEG4/ISO/SP": "MPEG-4 Part 2",
    b"V_MPEG2": "MPEG-2",
    b"V_THEORA": "Theora",
}

# Codecs no mainstream browser will decode from a plain <video> tag.
# HEVC is the interesting one: Firefox has no software decoder, and even
# with media.wmf.hevc.enabled it needs an OS hardware decoder, which
# commonly refuses High tier streams. Treat it as unplayable everywhere.
_UNPLAYABLE = {
    "HEVC",
    "Dolby Vision (HEVC)",
    "VVC/H.266",
    "MPEG-4 Part 2",
    "H.263",
    "MPEG-2",
    "ProRes",
    "Theora",  # only ever worked in WebM/Ogg, and not in Safari at all
}

# Container boxes worth descending into on the way to stsd.
_MP4_CONTAINERS = {b"moov", b"trak", b"mdia", b"minf", b"stbl", b"edts",
                   b"mvex", b"moof", b"traf"}

VIDEO_SUFFIXES = {".mp4", ".m4v", ".mov", ".webm", ".mkv"}

_MAX_BOX_SCAN = 4096          # boxes to walk before giving up on a level
_cache: dict = {}
_cache_lock = threading.Lock()


# ── MP4 ───────────────────────────────────────────────────────────────────────

def _read_box_header(fh):
    """Return (size, type, header_len) or None at EOF/garbage.

    Handles the 64-bit ``largesize`` form and size==0 ("to end of file"),
    which is what a streaming muxer writes for a trailing mdat.
    """
    head = fh.read(8)
    if len(head) < 8:
        return None
    size, btype = struct.unpack(">I4s", head)
    hlen = 8
    if size == 1:
        ext = fh.read(8)
        if len(ext) < 8:
            return None
        size = struct.unpack(">Q", ext)[0]
        hlen = 16
    elif size == 0:
        size = -1  # extends to EOF
    if size != -1 and size < hlen:
        return None
    return size, btype, hlen


def _read_mvhd_duration(fh, body: int):
    """Seconds from an mvhd box body, or None."""
    fh.seek(body)
    ver = fh.read(4)
    if len(ver) < 4:
        return None
    if ver[0] == 1:
        raw = fh.read(28)                      # ctime(8) mtime(8) ts(4) dur(8)
        if len(raw) < 28:
            return None
        timescale, duration = struct.unpack(">IQ", raw[16:28])
    else:
        raw = fh.read(16)                      # ctime(4) mtime(4) ts(4) dur(4)
        if len(raw) < 16:
            return None
        timescale, duration = struct.unpack(">II", raw[8:16])
    if not timescale or not duration or duration == 0xFFFFFFFF:
        return None
    return duration / timescale


def _find_mp4_stsd_formats(fh, start: int, end: int, depth: int = 0, info=None):
    """Walk the box tree from ``start`` collecting stsd sample entry formats.

    Returns a list of (handler_hint, format) tuples. The handler hint is the
    nearest enclosing ``hdlr`` subtype when one was seen, which distinguishes
    the video track from audio and subtitle tracks. ``info`` collects
    side-channel findings (currently the movie duration from mvhd).
    """
    out = []
    if depth > 8:
        return out
    pos = start
    handler = None
    for _ in range(_MAX_BOX_SCAN):
        if pos >= end:
            break
        fh.seek(pos)
        hdr = _read_box_header(fh)
        if not hdr:
            break
        size, btype, hlen = hdr
        box_end = end if size == -1 else min(pos + size, end)
        body = pos + hlen

        if btype == b"hdlr":
            # version/flags(4) predefined(4) handlertype(4)
            fh.seek(body + 8)
            handler = fh.read(4)
        elif btype == b"stsd":
            # version/flags(4) entry_count(4) then sample entries
            fh.seek(body + 8)
            raw = fh.read(min(256, max(box_end - body - 8, 0)))
            if len(raw) >= 8:
                fmt = raw[4:8]
                out.append((handler, fmt))
        elif btype == b"mvhd":
            if info is not None and info.get("duration") is None:
                info["duration"] = _read_mvhd_duration(fh, body)
        elif btype in _MP4_CONTAINERS:
            out.extend(_find_mp4_stsd_formats(fh, body, box_end, depth + 1, info))

        if size == -1:
            break
        pos = box_end
        if pos <= body - hlen:  # no forward progress; malformed
            break
    return out


def _probe_mp4(path: Path) -> tuple:
    """Return (video_codec_name, raw_fourcc, duration_seconds)."""
    size = path.stat().st_size
    info: dict = {"duration": None}
    with open(path, "rb") as fh:
        formats = _find_mp4_stsd_formats(fh, 0, size, info=info)
    video_fmt = None
    for handler, fmt in formats:
        if handler == b"vide":
            video_fmt = fmt
            break
    if video_fmt is None:
        # No hdlr seen (or an odd layout) — fall back to the first entry that
        # looks like a known video format.
        for _handler, fmt in formats:
            if fmt in _MP4_VIDEO_CODECS:
                video_fmt = fmt
                break
    if video_fmt is None:
        return None, None, info["duration"]
    return (_MP4_VIDEO_CODECS.get(video_fmt),
            video_fmt.decode("latin-1", "replace"),
            info["duration"])


# ── Matroska / WebM ───────────────────────────────────────────────────────────

def _probe_mkv(path: Path) -> tuple:
    """Cheap CodecID scan over the head of the file.

    A real EBML parse is overkill here: the Tracks element lives near the
    front in every file a browser or scraper produces, and the CodecID
    strings are unambiguous ASCII.
    """
    with open(path, "rb") as fh:
        head = fh.read(256 * 1024)
    best = None
    best_at = len(head) + 1
    for cid, name in _MKV_VIDEO_CODECS.items():
        at = head.find(cid)
        # V_VP8 is a prefix of nothing, but V_MPEG4/ISO/AVC vs .../ASP
        # both contain "V_MPEG4/ISO/" — exact keys avoid that collision,
        # and the earliest match wins so the first track decides.
        if at != -1 and at < best_at:
            best, best_at = (name, cid), at
    if not best:
        return None, None, None
    name, cid = best
    return name, cid.decode("ascii", "replace"), None


# ── Public API ────────────────────────────────────────────────────────────────

def probe(path) -> dict:
    """Identify the video codec in ``path``.

    Returns a dict with:
        codec     friendly codec name, or None if it could not be identified
        fourcc    the raw sample entry / CodecID the name came from
        playable  False only when the codec is known-unplayable in browsers
        reason    short human-readable note when not playable, else None
        duration  length in seconds when the container states it, else None

    Never raises: an unreadable or malformed file is reported as playable
    with codec None, so the caller renders a normal player.
    """
    path = Path(path)
    unknown = {"codec": None, "fourcc": None, "playable": True,
               "reason": None, "duration": None}
    try:
        st = path.stat()
    except OSError:
        return unknown
    key = (str(path), st.st_mtime_ns, st.st_size)
    with _cache_lock:
        hit = _cache.get(key)
    if hit is not None:
        return dict(hit)

    suffix = path.suffix.lower()
    try:
        if suffix in (".mp4", ".m4v", ".mov"):
            codec, fourcc, duration = _probe_mp4(path)
        elif suffix in (".webm", ".mkv"):
            codec, fourcc, duration = _probe_mkv(path)
        else:
            codec, fourcc, duration = None, None, None
    except Exception as exc:                     # noqa: BLE001 - never fail a page render
        log.debug("codec probe failed for %s: %s", path.name, exc)
        codec, fourcc, duration = None, None, None

    if codec is None:
        result = dict(unknown, duration=duration)
    elif codec in _UNPLAYABLE:
        result = {
            "codec": codec,
            "fourcc": fourcc,
            "playable": False,
            "reason": f"{codec} is not decodable by browsers",
            "duration": duration,
        }
    else:
        result = {"codec": codec, "fourcc": fourcc, "playable": True,
                  "reason": None, "duration": duration}

    with _cache_lock:
        if len(_cache) > 20000:
            _cache.clear()
        _cache[key] = dict(result)
    return dict(result)


def is_video(name) -> bool:
    return Path(name).suffix.lower() in VIDEO_SUFFIXES


if __name__ == "__main__":                        # pragma: no cover
    import sys
    for arg in sys.argv[1:]:
        print(arg, probe(arg))
