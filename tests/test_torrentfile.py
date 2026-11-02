import hashlib

import pytest

from kshow.torrentfile import info_hash

INFO = b"d6:lengthi12e4:name5:a.mp412:piece lengthi16384e6:pieces20:" + b"x" * 20 + b"e"


def test_info_hash_is_sha1_of_the_bencoded_info_dict():
    data = b"d8:announce14:udp://tracker/4:info" + INFO + b"e"
    assert info_hash(data) == hashlib.sha1(INFO).hexdigest()


def test_info_hash_ignores_keys_after_info():
    data = b"d4:info" + INFO + b"7:comment5:helloe"
    assert info_hash(data) == hashlib.sha1(INFO).hexdigest()


def test_v2_only_torrent_uses_truncated_sha256():
    info = b"d12:meta versioni2e4:name1:xe"
    assert info_hash(b"d4:info" + info + b"e") == hashlib.sha256(info).hexdigest()[:40]


@pytest.mark.parametrize(
    "data",
    [b"", b"not bencode", b"d8:announce3:fooe", b"d4:infoi3ee", b"d4:infod4:name", b"l4:infoe"],
)
def test_malformed_or_missing_info_gives_none(data):
    assert info_hash(data) is None
