from datetime import date
from kshow.releases import parse_release_name, parse_user_input, normalize_title

def test_parses_season_release():
    r = parse_release_name("신인감독 김연경 2.E03.261025.720p-NEXT")
    assert (r.title, r.season, r.episode) == ("신인감독 김연경", 2, 3)
    assert r.airdate == date(2026, 10, 25)
    assert (r.resolution, r.group) == ("720p", "NEXT")

def test_no_season_number_means_season_1():
    r = parse_release_name("신인감독 김연경.E09.251123.1080p-F1RST")
    assert (r.title, r.season, r.episode, r.resolution) == ("신인감독 김연경", 1, 9, "1080p")

def test_strips_extensions_and_torrent_suffix():
    r = parse_release_name("신인감독 김연경 2.E03.261025.720p-NEXT.mp4.torrent")
    assert r.episode == 3 and r.group == "NEXT"

def test_end_marker_and_no_group():
    r = parse_release_name("어떤 드라마.E16.END.261020.1080p")
    assert (r.episode, r.group, r.resolution) == (16, None, "1080p")

def test_unparseable_returns_none():
    assert parse_release_name("신인감독 김연경 2 전편 모음") is None
    assert parse_release_name("어떤 드라마.E01-E02.261020.720p-NEXT") is None  # multi-episode: ignored (ambiguous)

def test_invalid_date_keeps_release():
    r = parse_release_name("어떤 드라마.E01.269999.720p-NEXT")
    assert r.episode == 1 and r.airdate is None

def test_user_input_season_variants():
    assert parse_user_input("신인감독 김연경 2") == ("신인감독 김연경", 2)
    assert parse_user_input("신인감독 김연경 시즌2") == ("신인감독 김연경", 2)
    assert parse_user_input("신인감독 김연경 시즌 3 ") == ("신인감독 김연경", 3)
    assert parse_user_input("신인감독 김연경") == ("신인감독 김연경", 1)

def test_normalize_title():
    assert normalize_title("신인감독  김연경!") == normalize_title("신인감독김연경")
