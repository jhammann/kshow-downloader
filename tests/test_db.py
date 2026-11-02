import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from kshow.db import (
    Episode,
    Grab,
    Season,
    Show,
    SourceRow,
    get_setting,
    init_db,
    set_setting,
    utcnow,
)


def make_show() -> Show:
    return Show(search_title="신인감독 김연경", folder_name="The Rookie Coach")


def make_season(show: Show, number: int = 2) -> Season:
    return Season(show=show, number=number, release_season=number, folder=f"Season {number:02}")


def test_init_db_seeds_source_and_settings():
    Session = init_db("sqlite://")
    with Session() as s:
        sources = s.scalars(select(SourceRow)).all()
        assert [x.name for x in sources] == ["torrenttip"]
        assert sources[0].domain_template == "https://torrenttip{N}.top"
        assert sources[0].current_url == "https://torrenttip250.top"
        assert sources[0].priority == 1
        assert get_setting(s, "qbit_category") == "kshow"
        assert get_setting(s, "check_interval_minutes") == "20"
        assert get_setting(s, "path_map") == ""


def test_set_setting_overwrites():
    Session = init_db("sqlite://")
    with Session() as s:
        set_setting(s, "stall_hours", "12")
        set_setting(s, "stall_hours", "24")
        assert get_setting(s, "stall_hours") == "24"


def test_show_defaults():
    Session = init_db("sqlite://")
    with Session() as s:
        show = make_show()
        s.add(show)
        s.commit()
        assert show.status == "following"
        assert show.added_at is not None
        assert show.max_known_season == 0
        assert show.poster_url is None and show.tvdb_id is None


def test_season_and_episode_defaults_and_relationships():
    Session = init_db("sqlite://")
    with Session() as s:
        show = make_show()
        s2 = make_season(show, 2)
        s1 = make_season(show, 1)
        ep = Episode(season=s2, number=3)
        s.add(show)
        s.commit()
        s.expire_all()
        assert show.seasons == [s1, s2]  # ordered by number when loaded
        assert s2.show is show and ep.season is s2 and s2.episodes == [ep]
        assert ep.season_id == s2.id
        assert (s2.followed, s2.followed_since, s2.first_aired) == (False, None, None)
        assert (ep.state, ep.monitored, ep.overdue_notified) == ("wanted", False, False)


def test_duplicate_season_number_rejected():
    Session = init_db("sqlite://")
    with Session() as s:
        show = make_show()
        make_season(show, 1)
        make_season(show, 1)
        s.add(show)
        with pytest.raises(IntegrityError):
            s.commit()


def test_duplicate_episode_number_rejected():
    Session = init_db("sqlite://")
    with Session() as s:
        season = make_season(make_show())
        s.add(season)
        s.commit()
        s.add_all([Episode(season_id=season.id, number=1), Episode(season_id=season.id, number=1)])
        with pytest.raises(IntegrityError):
            s.commit()


def test_init_db_twice_does_not_duplicate(tmp_path):
    url = f"sqlite:///{tmp_path / 'k.db'}"
    init_db(url)
    Session = init_db(url)
    with Session() as s:
        assert len(s.scalars(select(SourceRow)).all()) == 1


def test_timestamps_are_naive_utc():
    Session = init_db("sqlite://")
    with Session() as s:
        show = make_show()
        ep = Episode(season=make_season(show), number=1)
        s.add(ep)
        s.commit()
        grab = Grab(episode_id=ep.id, post_url="u", release_name="r", qbit_tag="t")
        s.add(grab)
        s.commit()
        assert abs((utcnow() - grab.last_progress_at).total_seconds()) < 5
        assert abs((utcnow() - grab.created_at).total_seconds()) < 5
        assert abs((utcnow() - show.added_at).total_seconds()) < 5


def test_grab_ids_are_never_reused():
    Session = init_db("sqlite://")
    with Session() as s:
        ep = Episode(season=make_season(make_show()), number=1)
        s.add(ep)
        s.flush()
        first = Grab(episode_id=ep.id, post_url="u", release_name="r", qbit_tag="")
        s.add(first)
        s.commit()
        first_id = first.id
        s.delete(first)
        s.commit()
        second = Grab(episode_id=ep.id, post_url="u", release_name="r", qbit_tag="")
        s.add(second)
        s.commit()
        assert second.id > first_id


_V2_SHOWS_WITHOUT_POSTER = """
CREATE TABLE shows (
    id INTEGER NOT NULL PRIMARY KEY,
    search_title VARCHAR NOT NULL,
    tvdb_id INTEGER,
    folder_name VARCHAR NOT NULL,
    status VARCHAR NOT NULL,
    added_at DATETIME NOT NULL,
    max_known_season INTEGER NOT NULL
)
"""


def test_init_db_adds_missing_nullable_columns_to_existing_db(tmp_path):
    import sqlite3

    path = tmp_path / "old.db"
    con = sqlite3.connect(path)
    con.execute(_V2_SHOWS_WITHOUT_POSTER)
    con.execute(
        "INSERT INTO shows VALUES (1, '놀면 뭐하니', 5, 'Hangout', 'following',"
        " '2026-10-01 10:00:00', 1)"
    )
    con.execute("PRAGMA user_version = 2")
    con.commit()
    con.close()

    url = f"sqlite:///{path}"
    init_db(url)
    Session = init_db(url)  # idempotent: the second run finds nothing to add

    with Session() as s:
        show = s.get(Show, 1)
        assert show.folder_name == "Hangout" and show.tvdb_id == 5
        assert show.poster_url is None
        show.poster_url = "https://artworks.thetvdb.com/p.jpg"
        s.commit()
    con = sqlite3.connect(path)
    cols = [row[1] for row in con.execute("PRAGMA table_info(shows)")]
    con.close()
    assert cols.count("poster_url") == 1


def test_today_kst_is_the_date_in_seoul(monkeypatch):
    from datetime import date, datetime, timezone

    from kshow import clock

    class FakeDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            # 16:30 UTC on 1 Nov is 01:30 on 2 Nov in Seoul.
            return datetime(2026, 11, 1, 16, 30, tzinfo=timezone.utc).astimezone(tz)

    monkeypatch.setattr(clock, "datetime", FakeDatetime)
    assert clock.today_kst() == date(2026, 11, 2)
