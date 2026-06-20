"""Quick debug script: test metadata detection on actual archived images."""
import sys
from pathlib import Path
import struct
import zlib
import numpy as np
from PIL import Image

def test_image(filepath: Path):
    print(f"\n=== {filepath.name} ===")

    img = Image.open(filepath)
    print(f"  Mode: {img.mode}, Size: {img.size}")

    arr = np.array(img.convert('RGBA'))
    height, width = arr.shape[:2]
    arr_T = arr.transpose(1, 0, 2)

    has_alpha = bool(np.any(arr_T[:, :, 3] < 255))
    print(f"  has_alpha: {has_alpha}")

    if has_alpha:
        unique_alpha = np.unique(arr_T[:, :, 3])
        print(f"  alpha range: {unique_alpha.min()} - {unique_alpha.max()}, unique values: {len(unique_alpha)}")

    # Alpha LSBs
    a_bits = (arr_T[:, :, 3] & 1).flatten()
    r_bits = (arr_T[:, :, 0] & 1).flatten()
    g_bits = (arr_T[:, :, 1] & 1).flatten()
    b_bits = (arr_T[:, :, 2] & 1).flatten()

    n_pixels = width * height
    rgb_bits = np.empty(n_pixels * 3, dtype=np.uint8)
    rgb_bits[0::3] = r_bits
    rgb_bits[1::3] = g_bits
    rgb_bits[2::3] = b_bits

    sig_len = len('stealth_pnginfo') * 8  # 112

    # Show what the first 112 alpha bits decode to
    if has_alpha and len(a_bits) >= sig_len:
        alpha_sig_bytes = np.packbits(a_bits[:sig_len]).tobytes()
        print(f"  Alpha sig (14 bytes): {alpha_sig_bytes!r}")

    # Show what the first 112 RGB bits decode to
    if len(rgb_bits) >= sig_len:
        rgb_sig_bytes = np.packbits(rgb_bits[:sig_len]).tobytes()
        print(f"  RGB sig (14 bytes):   {rgb_sig_bytes!r}")

    # Check standard PNG chunks
    if filepath.suffix.lower() == '.png':
        try:
            with open(filepath, 'rb') as f:
                sig = f.read(8)
                if sig == b'\x89PNG\r\n\x1a\n':
                    while True:
                        chunk_header = f.read(8)
                        if len(chunk_header) < 8:
                            break
                        length = struct.unpack('>I', chunk_header[:4])[0]
                        chunk_type = chunk_header[4:8].decode('latin1')
                        data = f.read(length)
                        f.read(4)  # CRC
                        if chunk_type == 'IEND':
                            break
                        if chunk_type in ('tEXt', 'iTXt'):
                            null_pos = data.find(b'\x00')
                            if null_pos > 0:
                                keyword = data[:null_pos].decode('latin1')
                                print(f"  PNG chunk {chunk_type}: keyword={keyword!r}")
        except Exception as e:
            print(f"  PNG chunk read error: {e}")


if __name__ == '__main__':
    # Find some PNG images in the archive
    archive = Path('/data/archive')

    pngs = list(archive.rglob('images/*.png'))[:5]
    if not pngs:
        print("No PNG files found!")
        sys.exit(1)

    print(f"Testing {len(pngs)} PNG files from archive...")
    for p in pngs:
        test_image(p)
