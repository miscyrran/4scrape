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
    Supports: stealth_pnginfo, stealth_pngcomp, stealth_rgbinfo, stealth_rgbcomp
    """
    try:
        img = Image.open(filepath)

        # Convert to RGB/RGBA if needed
        if img.mode not in ('RGB', 'RGBA'):
            img = img.convert('RGBA')

        width, height = img.size
        pixels = img.load()

        # Try different stealth signatures
        signatures = [
            (b'stealth_pnginfo', False, 'RGB'),   # Uncompressed RGB
            (b'stealth_pngcomp', True, 'RGB'),    # Compressed RGB
            (b'stealth_rgbinfo', False, 'RGB'),   # Uncompressed RGB
            (b'stealth_rgbcomp', True, 'RGB'),    # Compressed RGB
        ]

        for signature, compressed, mode in signatures:
            try:
                # Extract LSB data from pixels
                binary_data = []
                sig_len = len(signature) * 8

                # Read signature first
                bit_index = 0
                for y in range(height):
                    for x in range(width):
                        if bit_index >= sig_len:
                            break
                        pixel = pixels[x, y]

                        # Extract LSB from RGB channels
                        for channel in range(3):
                            if bit_index < sig_len:
                                binary_data.append(pixel[channel] & 1)
                                bit_index += 1
                    if bit_index >= sig_len:
                        break

                # Convert bits to bytes and check signature
                sig_bytes = bytes([sum([binary_data[i*8+j] << j for j in range(8)]) for i in range(len(signature))])
                if sig_bytes != signature:
                    continue

                # Signature matched! Now read the data length (4 bytes = 32 bits)
                binary_data = []
                for y in range(height):
                    for x in range(width):
                        pixel = pixels[x, y]
                        for channel in range(3):
                            binary_data.append(pixel[channel] & 1)
                            if len(binary_data) >= (sig_len + 32 + 8192*8):  # Sig + length + reasonable data
                                break
                        if len(binary_data) >= (sig_len + 32 + 8192*8):
                            break
                    if len(binary_data) >= (sig_len + 32 + 8192*8):
                        break

                # Extract length (4 bytes after signature)
                length_bits = binary_data[sig_len:sig_len+32]
                data_length = sum([length_bits[i] << i for i in range(32)])

                # Sanity check on length
                if data_length <= 0 or data_length > 10*1024*1024:  # Max 10MB
                    continue

                # Extract the actual data
                data_start = sig_len + 32
                data_end = data_start + (data_length * 8)

                if data_end > len(binary_data):
                    # Need to read more pixels
                    continue

                data_bits = binary_data[data_start:data_end]
                data_bytes = bytes([sum([data_bits[i*8+j] << j for j in range(8)])
                                   for i in range(data_length)])

                # Decompress if needed
                if compressed:
                    try:
                        data_bytes = zlib.decompress(data_bytes)
                    except:
                        continue

                # Try to decode as UTF-8
                metadata_text = data_bytes.decode('utf-8', errors='ignore')
                if metadata_text and len(metadata_text.strip()) > 10:
                    return metadata_text

            except Exception:
                continue

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
