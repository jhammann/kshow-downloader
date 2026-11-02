"""Read the info-hash from a .torrent file, the ID qBittorrent uses for a torrent."""

import hashlib


def _skip(data: bytes, i: int) -> int:
    """Return the index just past the bencoded value starting at ``i``."""
    c = data[i : i + 1]
    if c == b"i":
        return data.index(b"e", i) + 1
    if c in (b"l", b"d"):
        i += 1
        while data[i : i + 1] != b"e":
            if i >= len(data):
                raise ValueError("unterminated container")
            i = _skip(data, i)
        return i + 1
    if c.isdigit():
        colon = data.index(b":", i)
        end = colon + 1 + int(data[i:colon])
        if end > len(data):
            raise ValueError("truncated string")
        return end
    raise ValueError("not bencode")


def info_hash(data: bytes) -> str | None:
    """qBittorrent's ID for a torrent: SHA-1 of the info dict, or truncated SHA-256 for a
    v2-only torrent. None when the data isn't a torrent with an info dict."""
    try:
        if data[:1] != b"d":
            return None
        i = 1
        while data[i : i + 1] != b"e":
            key_end = _skip(data, i)
            key = data[data.index(b":", i) + 1 : key_end]
            value_end = _skip(data, key_end)
            if key == b"info":
                info = data[key_end:value_end]
                if info[:1] != b"d":
                    return None
                if b"6:pieces" not in info and b"12:meta versioni2e" in info:
                    return hashlib.sha256(info).hexdigest()[:40]
                return hashlib.sha1(info).hexdigest()
            i = value_end
        return None
    except (ValueError, IndexError):
        return None
