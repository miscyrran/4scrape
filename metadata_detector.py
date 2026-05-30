"""
SD Metadata Detection Module for 4scrape
Detects and extracts Stable Diffusion metadata from images
"""
import json
import struct
import zlib
from pathlib import Path
from typing import Optional, Dict, Any
from PIL import Image
import re


def read_png_chunks(filepath: Path) -> dict:
    """Read PNG text chunks containing metadata."""
    try:
        with open(filepath, 'rb') as f:
            # Verify PNG signature
            signature = f.read(8)
            if signature != b'\x89PNG\r\n\x1a\n':
                return {}

            chunks = {}
            while True:
                # Read chunk length and type
                chunk_data = f.read(8)
                if len(chunk_data) < 8:
                    break

                length = struct.unpack('>I', chunk_data[:4])[0]
                chunk_type = chunk_data[4:8].decode('latin1')

                # Read chunk data and CRC
                data = f.read(length)
                crc = f.read(4)

                if chunk_type == 'IEND':
                    break

                # Store text chunks
                if chunk_type == 'tEXt':
                    try:
                        null_pos = data.find(b'\x00')
                        if null_pos > 0:
                            keyword = data[:null_pos].decode('latin1')
                            text = data[null_pos+1:].decode('utf-8', errors='ignore')
                            chunks[keyword] = text
                    except:
                        pass

                elif chunk_type == 'zTXt':
                    try:
                        null_pos = data.find(b'\x00')
                        if null_pos > 0:
                            keyword = data[:null_pos].decode('latin1')
                            # 1 byte compression method after null, then compressed data
                            compressed_data = data[null_pos+2:]
                            text = zlib.decompress(compressed_data).decode('utf-8', errors='ignore')
                            chunks[keyword] = text
                    except:
                        pass

                elif chunk_type == 'iTXt':
                    try:
                        null_pos = data.find(b'\x00')
                        if null_pos > 0:
                            keyword = data[:null_pos].decode('latin1')
                            # compression_flag, compression_method, language_tag\0, translated_keyword\0, text
                            rest = data[null_pos+1:]
                            compression_flag = rest[0]
                            compression_method = rest[1]
                            rest = rest[2:]
                            # skip language tag
                            lang_end = rest.find(b'\x00')
                            rest = rest[lang_end+1:]
                            # skip translated keyword
                            trans_end = rest.find(b'\x00')
                            text_data = rest[trans_end+1:]
                            if compression_flag == 1:
                                text_data = zlib.decompress(text_data)
                            chunks[keyword] = text_data.decode('utf-8', errors='ignore')
                    except:
                        pass

            return chunks
    except Exception as e:
        return {}


def _bits_to_int(bits) -> int:
    """Convert MSB-first bit array to integer."""
    result = 0
    for b in bits:
        result = (result << 1) | int(b)
    return result


def _bits_to_bytes(bits) -> bytes:
    """Convert MSB-first bit array to bytes (truncates to multiple of 8)."""
    import numpy as np
    n = (len(bits) // 8) * 8
    arr = np.array(bits[:n], dtype=np.uint8)
    return np.packbits(arr).tobytes()


def read_stealth_metadata(filepath: Path) -> Optional[str]:
    """
    Extract stealth metadata from LSB-encoded pixels using numpy for speed.
    Matches JS implementation: column-major (x outer, y inner), MSB-first bits,
    alpha channel for stealth_pnginfo/comp, RGB for stealth_rgbinfo/comp.
    paramLen is in bits.
    """
    try:
        import numpy as np

        img = Image.open(filepath)
        arr = np.array(img.convert('RGBA'))  # (height, width, 4)
        height, width = arr.shape[:2]

        # Column-major order: transpose to (width, height, channels) then flatten
        arr_T = arr.transpose(1, 0, 2)  # (width, height, 4)

        # Extract LSBs for all channels in column-major order
        a_bits = (arr_T[:, :, 3] & 1).flatten()  # alpha LSBs
        r_bits = (arr_T[:, :, 0] & 1).flatten()
        g_bits = (arr_T[:, :, 1] & 1).flatten()
        b_bits = (arr_T[:, :, 2] & 1).flatten()

        # Interleaved RGB bits: r0,g0,b0, r1,g1,b1, ...
        n_pixels = width * height
        rgb_bits = np.empty(n_pixels * 3, dtype=np.uint8)
        rgb_bits[0::3] = r_bits
        rgb_bits[1::3] = g_bits
        rgb_bits[2::3] = b_bits

        has_alpha = bool(np.any(arr_T[:, :, 3] < 255))
        sig_len = len('stealth_pnginfo') * 8  # 112 bits

        # --- Try alpha channel (stealth_pnginfo / stealth_pngcomp) ---
        if has_alpha and len(a_bits) >= sig_len + 32:
            sig_bytes = _bits_to_bytes(a_bits[:sig_len])
            decoded_sig = sig_bytes.decode('latin1', errors='ignore')
            if decoded_sig in ('stealth_pnginfo', 'stealth_pngcomp'):
                compressed = (decoded_sig == 'stealth_pngcomp')
                param_len = _bits_to_int(a_bits[sig_len:sig_len + 32])
                data_start = sig_len + 32
                data_end = data_start + param_len
                if param_len > 0 and len(a_bits) >= data_end:
                    byte_data = _bits_to_bytes(a_bits[data_start:data_end])
                    if compressed:
                        byte_data = zlib.decompress(byte_data)
                    decoded = byte_data.decode('utf-8', errors='ignore')
                    if decoded and len(decoded.strip()) > 10:
                        return decoded

        # --- Try RGB channels (stealth_rgbinfo / stealth_rgbcomp) ---
        if len(rgb_bits) >= sig_len + 32:
            sig_bytes = _bits_to_bytes(rgb_bits[:sig_len])
            decoded_sig = sig_bytes.decode('latin1', errors='ignore')
            if decoded_sig in ('stealth_rgbinfo', 'stealth_rgbcomp'):
                compressed = (decoded_sig == 'stealth_rgbcomp')
                # JS reads 33 bits, takes first 32 as length
                param_len = _bits_to_int(rgb_bits[sig_len:sig_len + 32])
                data_start = sig_len + 32  # 33rd bit onwards is data
                data_end = data_start + param_len
                if param_len > 0 and len(rgb_bits) >= data_end:
                    byte_data = _bits_to_bytes(rgb_bits[data_start:data_end])
                    if compressed:
                        byte_data = zlib.decompress(byte_data)
                    decoded = byte_data.decode('utf-8', errors='ignore')
                    if decoded and len(decoded.strip()) > 10:
                        return decoded

        return None

    except Exception:
        return None


def read_jpeg_exif(filepath: Path) -> Optional[str]:
    """Read EXIF UserComment from JPEG/WebP."""
    try:
        from PIL.ExifTags import TAGS

        img = Image.open(filepath)
        exif = img.getexif()

        if exif:
            for tag_id, value in exif.items():
                tag = TAGS.get(tag_id, tag_id)
                if tag == 'UserComment':
                    if isinstance(value, bytes):
                        # Skip the encoding header (usually first 8 bytes)
                        return value[8:].decode('utf-8', errors='ignore').strip('\x00')
                    return str(value)
        return None
    except Exception:
        return None


def extract_metadata(filepath: Path) -> Optional[str]:
    """
    Extract SD metadata from an image file.
    Returns the metadata string if found, None otherwise.
    """
    import logging
    if not filepath.exists():
        return None

    ext = filepath.suffix.lower()
    metadata = None

    try:
        # PNG files - check tEXt chunks
        if ext == '.png':
            chunks = read_png_chunks(filepath)
            if chunks:
                logging.debug(f"[MetadataDetector] {filepath.name}: PNG chunks found: {list(chunks.keys())}")
            else:
                logging.debug(f"[MetadataDetector] {filepath.name}: no PNG text chunks")

            # NovelAI format
            if 'Comment' in chunks and 'Description' in chunks and 'Software' in chunks:
                try:
                    comment_data = json.loads(chunks['Comment'])
                    source = chunks.get('Source', 'Unknown')
                    return f"Version: {source}\n\n{json.dumps(comment_data, indent=2)}"
                except:
                    pass

            # A1111 format
            if 'parameters' in chunks:
                return chunks['parameters']

            # ComfyUI format
            if 'prompt' in chunks or 'workflow' in chunks:
                parts = []
                if 'prompt' in chunks:
                    parts.append(f"Prompt:\n{chunks['prompt']}")
                if 'workflow' in chunks:
                    parts.append(f"Workflow:\n{chunks['workflow']}")
                return "\n\n".join(parts)

            # Dream format
            if 'Dream' in chunks:
                return chunks['Dream']

        # JPEG/WebP - check EXIF
        elif ext in ('.jpg', '.jpeg', '.webp', '.avif'):
            exif_data = read_jpeg_exif(filepath)
            if exif_data:
                return exif_data

        # Check for stealth metadata (works on all image types)
        stealth_data = read_stealth_metadata(filepath)
        if stealth_data:
            return stealth_data

    except Exception as e:
        pass

    return None


def has_metadata(filepath: Path) -> bool:
    """Quick check if an image has SD metadata."""
    return extract_metadata(filepath) is not None


def scan_thread_images(thread_dir: Path) -> Dict[str, bool]:
    """
    Scan all images in a thread directory for metadata.
    Returns a dict mapping filename -> has_metadata bool.
    """
    results = {}
    images_dir = thread_dir / "images"

    if not images_dir.exists():
        return results

    for img_file in images_dir.iterdir():
        if img_file.suffix.lower() in ('.png', '.jpg', '.jpeg', '.webp', '.avif'):
            results[img_file.name] = has_metadata(img_file)

    return results


def get_metadata_cache_path(thread_dir: Path) -> Path:
    """Get the path to the metadata cache file for a thread."""
    return thread_dir / "metadata_cache.json"


def load_metadata_cache(thread_dir: Path) -> Dict[str, bool]:
    """Load cached metadata scan results."""
    cache_file = get_metadata_cache_path(thread_dir)
    if cache_file.exists():
        try:
            with open(cache_file, 'r') as f:
                return json.load(f)
        except:
            pass
    return {}


def save_metadata_cache(thread_dir: Path, cache: Dict[str, bool]):
    """Save metadata scan results to cache."""
    cache_file = get_metadata_cache_path(thread_dir)
    try:
        with open(cache_file, 'w') as f:
            json.dump(cache, f, indent=2)
    except:
        pass


def get_thread_metadata_status(thread_dir: Path, force_rescan: bool = False) -> Dict[str, bool]:
    """
    Get metadata status for all images in a thread.
    Uses cache if available, otherwise scans and caches.
    """
    import logging

    if not force_rescan:
        cache = load_metadata_cache(thread_dir)
        if cache:
            logging.info(f"[MetadataDetector] Loaded cache for {thread_dir.name}: {len(cache)} entries")
            return cache

    # Scan and cache
    logging.info(f"[MetadataDetector] Scanning images in {thread_dir.name}...")
    results = scan_thread_images(thread_dir)
    logging.info(f"[MetadataDetector] Scan complete: {sum(1 for v in results.values() if v)} of {len(results)} have metadata")
    save_metadata_cache(thread_dir, results)
    return results
