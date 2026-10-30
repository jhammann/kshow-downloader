import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from kshow.db import Episode, Grab, Show, SourceRow, get_setting, init_db, set_setting, utcnow


def make_show() -> Show:
    return Show(
        search_title="신인감독 김연경",
        release_season=2,
        tvdb_season=2,
        folder_name="The Rookie Coach",
        season_folder="Season 02",
    )


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


def test_duplicate_episode_number_rejected():
    Session = init_db("sqlite://")
    with Session() as s:
        show = make_show()
        s.add(show)
        s.commit()
        s.add_all([Episode(show_id=show.id, number=1), Episode(show_id=show.id, number=1)])
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
        s.add(show)
        s.commit()
        ep = Episode(show_id=show.id, number=1)
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
        show = make_show()
        s.add(show)
        s.flush()
        ep = Episode(show_id=show.id, number=1)
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
