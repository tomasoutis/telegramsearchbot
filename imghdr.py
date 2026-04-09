"""
Minimal imghdr shim for environments missing the stdlib module.
Provides a lightweight `what()` implementation detecting common image types.
"""
from typing import Optional

def _is_jpeg(h: bytes) -> bool:
    return len(h) >= 2 and h[0] == 0xFF and h[1] == 0xD8

def _is_png(h: bytes) -> bool:
    return h.startswith(b"\x89PNG\r\n\x1a\n")

def _is_gif(h: bytes) -> bool:
    return h.startswith(b"GIF87a") or h.startswith(b"GIF89a")

def _is_bmp(h: bytes) -> bool:
    return h.startswith(b"BM")

def _is_webp(h: bytes) -> bool:
    return len(h) >= 12 and h[0:4] == b"RIFF" and h[8:12] == b"WEBP"

def what(file, h: Optional[bytes] = None) -> Optional[str]:
    """Return a string describing the image type, or None.

    `file` may be a filename, a file object, or bytes. If `h` is provided,
    it's used as the header bytes.
    """
    header = h
    # If file is bytes/bytearray, treat as header
    if isinstance(file, (bytes, bytearray)) and header is None:
        header = bytes(file)
    elif header is None:
        # If file is a path
        try:
            if isinstance(file, str):
                with open(file, 'rb') as f:
                    header = f.read(32)
            else:
                # file-like object
                read = getattr(file, 'read', None)
                if callable(read):
                    pos = None
                    try:
                        pos = file.tell()
                    except Exception:
                        pos = None
                    header = file.read(32)
                    try:
                        if pos is not None:
                            file.seek(pos)
                    except Exception:
                        pass
        except Exception:
            header = None

    if not header:
        return None

    if _is_jpeg(header):
        return 'jpeg'
    if _is_png(header):
        return 'png'
    if _is_gif(header):
        return 'gif'
    if _is_bmp(header):
        return 'bmp'
    if _is_webp(header):
        return 'webp'
    return None
