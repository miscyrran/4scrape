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

                # Store tEXt and iTXt chunks
                if chunk_type in ('tEXt', 'iTXt'):
                    try:
                        # Split on null byte to get keyword and text
                        null_pos = data.find(b'\x00')
                        if null_pos > 0:
                            keyword = data[:null_pos].decode('latin1')
                            text = data[null_pos+1:].decode('utf-8', errors='ignore')
                            chunks[keyword] = text
                    except:
                        pass

            return chunks
    except Exception as e:
        return {}


def read_stealth_metadata(filepath: Path) -> Optional[str]:
    """
    Extract stealth metadata from LSB-encoded pixels.
    Matches the JavaScript implementation exactly:
    - Column-major iteration (x outer, y inner)
    - Alpha channel for stealth_pnginfo/comp, RGB for stealth_rgbinfo/comp
    - MSB-first bit strings, paramLen is in bits
    """
    try:
        img = Image.open(filepath)
        img_rgba = img.convert('RGBA')
        width, height = img_rgba.size
        pixels = list(img_rgba.getdata())  # flat list of (R,G,B,A) tuples

        # Detect if image has non-trivial alpha
        has_alpha = any(pixels[i][3] < 255 for i in range(min(len(pixels), 1000)))

        sig_length_bits = len('stealth_pnginfo') * 8

        mode = None
        compressed = False
        buffer_a = ''
        buffer_rgb = ''
        index_a = 0
        index_rgb = 0
        sig_confirmed = False
        confirming_signature = True
        reading_param_len = False
        reading_param = False
        read_end = False
        param_len = 0
        binary_data = ''

        for x in range(width):
            for y in range(height):
                r, g, b, a = pixels[y * width + x]

                if has_alpha:
                    buffer_a += str(a & 1)
                    index_a += 1

                buffer_rgb += str(r & 1)
                buffer_rgb += str(g & 1)
                buffer_rgb += str(b & 1)
                index_rgb += 3

                if confirming_signature:
                    if has_alpha and index_a == sig_length_bits:
                        decoded_sig = bytes([int(buffer_a[i:i+8], 2) for i in range(0, len(buffer_a), 8)]).decode('latin1')
                        if decoded_sig in ('stealth_pnginfo', 'stealth_pngcomp'):
                            confirming_signature = False
                            sig_confirmed = True
                            reading_param_len = True
                            mode = 'alpha'
                            compressed = (decoded_sig == 'stealth_pngcomp')
                            buffer_a = ''
                            index_a = 0
                        else:
                            read_end = True
                            break
                    elif index_rgb == sig_length_bits:
                        decoded_sig = bytes([int(buffer_rgb[i:i+8], 2) for i in range(0, len(buffer_rgb), 8)]).decode('latin1')
                        if decoded_sig in ('stealth_rgbinfo', 'stealth_rgbcomp'):
                            confirming_signature = False
                            sig_confirmed = True
                            reading_param_len = True
                            mode = 'rgb'
                            compressed = (decoded_sig == 'stealth_rgbcomp')
                            buffer_rgb = ''
                            index_rgb = 0

                elif reading_param_len:
                    if mode == 'alpha' and index_a == 32:
                        param_len = int(buffer_a, 2)
                        reading_param_len = False
                        reading_param = True
                        buffer_a = ''
                        index_a = 0
                    elif mode != 'alpha' and index_rgb == 33:
                        param_len = int(buffer_rgb[:-1], 2)
                        reading_param_len = False
                        reading_param = True
                        buffer_rgb = buffer_rgb[-1]
                        index_rgb = 1

                elif reading_param:
                    if mode == 'alpha' and index_a == param_len:
                        binary_data = buffer_a
                        read_end = True
                        break
                    elif mode != 'alpha' and index_rgb >= param_len:
                        diff = param_len - index_rgb
                        if diff < 0:
                            buffer_rgb = buffer_rgb[:diff]
                        binary_data = buffer_rgb
                        read_end = True
                        break
                else:
                    read_end = True
                    break

            if read_end:
                break

        if sig_confirmed and binary_data:
            byte_data = bytes([int(binary_data[i:i+8], 2) for i in range(0, len(binary_data) - len(binary_data) % 8, 8)])
            if compressed:
                decoded = zlib.decompress(byte_data).decode('utf-8', errors='ignore')
            else:
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
    if not filepath.exists():
        return None

    ext = filepath.suffix.lower()
    metadata = None

    try:
        # PNG files - check tEXt chunks
        if ext == '.png':
            chunks = read_png_chunks(filepath)

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
