import errno
import os
import shutil
from pathlib import Path

import pytest

from kshow.library import (
    existing_episodes,
    move_into_library,
    season_folder_name,
    suggest_folder,
    target_path,
)


def test_recognises_manually_filed_release_names(tmp_path):
    d = tmp_path / "The Wonder Coach" / "Season 2"; d.mkdir(parents=True)
    for e in (1, 2, 3):
        (d / f"신인감독 김연경 2.E0{e}.2610{10+e}.720p-NEXT.mp4").touch()
    (d / "notes.txt").touch()
    assert existing_episodes(d, 2) == {1, 2, 3}


def test_recognises_sxxeyy(tmp_path):
    (tmp_path / "The Wonder Coach - S02E04.mkv").touch()
    (tmp_path / "Other - S01E05.mkv").touch()
    assert existing_episodes(tmp_path, 2) == {4}


def test_sxxeyy_is_case_insensitive_and_needs_video_suffix(tmp_path):
    (tmp_path / "show - s02e07.MKV").touch()
    (tmp_path / "show - S02E08.srt").touch()
    assert existing_episodes(tmp_path, 2) == {7}


def test_release_names_count_regardless_of_season_number(tmp_path):
    (tmp_path / "신인감독 김연경 1.E05.261015.720p-NEXT.mp4").touch()
    assert existing_episodes(tmp_path, 2) == {5}


def test_missing_season_dir_is_empty(tmp_path):
    assert existing_episodes(tmp_path / "nope", 1) == set()


def test_suggest_folder_ignores_year_and_case(tmp_path):
    (tmp_path / "The Wonder Coach").mkdir()
    assert suggest_folder(tmp_path, "the wonder coach (2025)") == "The Wonder Coach"
    assert suggest_folder(tmp_path, "Something Else") is None


def test_suggest_folder_strips_year_from_existing_folder(tmp_path):
    (tmp_path / "The Wonder Coach (2025)").mkdir()
    assert suggest_folder(tmp_path, "The Wonder Coach") == "The Wonder Coach (2025)"


def test_reuses_existing_season_folder(tmp_path):
    (tmp_path / "Season 2").mkdir()
    assert season_folder_name(tmp_path, 2) == "Season 2"
    assert season_folder_name(tmp_path, 3) == "Season 03"
    (tmp_path / "Season 04").mkdir()
    assert season_folder_name(tmp_path, 4) == "Season 04"


def test_target_path(tmp_path):
    assert target_path(tmp_path, "The Wonder Coach", "Season 2", 2, 4, ".mp4") == \
        tmp_path / "The Wonder Coach" / "Season 2" / "The Wonder Coach - S02E04.mp4"


def test_move_creates_parents(tmp_path):
    src = tmp_path / "a.mp4"; src.write_text("x")
    dest = tmp_path / "x" / "y" / "b.mp4"
    assert move_into_library(src, dest) == dest
    assert dest.read_text() == "x" and not src.exists()


def test_move_never_overwrites(tmp_path):
    src = tmp_path / "a.mp4"; src.write_text("x")
    dest = tmp_path / "out" / "b.mp4"; dest.parent.mkdir(); dest.write_text("old")
    with pytest.raises(FileExistsError):
        move_into_library(src, dest)
    assert dest.read_text() == "old"


def _force_exdev(monkeypatch):
    def rename(src, dst, *a, **kw):
        raise OSError(errno.EXDEV, "cross-device link")

    monkeypatch.setattr(os, "rename", rename)


def test_move_across_devices(tmp_path, monkeypatch):
    _force_exdev(monkeypatch)
    src = tmp_path / "dl" / "ep.mp4"
    src.parent.mkdir()
    src.write_bytes(b"video")
    dest = tmp_path / "tv" / "Show" / "Season 01" / "Show - S01E01.mp4"
    assert move_into_library(src, dest) == dest
    assert dest.read_bytes() == b"video"
    assert not src.exists()
    assert [p.name for p in dest.parent.iterdir()] == [dest.name]


def test_failed_cross_device_copy_leaves_nothing(tmp_path, monkeypatch):
    _force_exdev(monkeypatch)

    def broken_copy(fsrc, fdst, *a, **kw):
        fdst.write(fsrc.read(2))
        fdst.flush()
        raise OSError(errno.ENOSPC, "disk full")

    monkeypatch.setattr(shutil, "copyfileobj", broken_copy)
    src = tmp_path / "ep.mp4"
    src.write_bytes(b"video")
    dest = tmp_path / "tv" / "Show - S01E01.mp4"
    with pytest.raises(OSError):
        move_into_library(src, dest)
    assert list((tmp_path / "tv").iterdir()) == []
    assert src.read_bytes() == b"video"


def test_broken_symlink_counts_as_existing(tmp_path):
    src = tmp_path / "ep.mp4"
    src.write_bytes(b"v")
    dest = tmp_path / "dest.mp4"
    dest.symlink_to(tmp_path / "nowhere")
    with pytest.raises(FileExistsError):
        move_into_library(src, dest)
    assert dest.is_symlink() and src.exists()


def test_cross_device_move_ignores_planted_temp_symlink(tmp_path, monkeypatch):
    _force_exdev(monkeypatch)
    victim = tmp_path / "victim.txt"
    victim.write_bytes(b"precious")
    src = tmp_path / "dl" / "ep.mp4"
    src.parent.mkdir()
    src.write_bytes(b"video")
    season = tmp_path / "tv" / "Season 01"
    season.mkdir(parents=True)
    dest = season / "Show - S01E01.mp4"
    planted = season / f".{dest.name}.partial"
    planted.symlink_to(victim)

    move_into_library(src, dest)

    assert victim.read_bytes() == b"precious"
    assert planted.is_symlink() and planted.resolve() == victim.resolve()
    assert dest.read_bytes() == b"video" and not dest.is_symlink()
    assert sorted(p.name for p in season.iterdir()) == sorted([dest.name, planted.name])
