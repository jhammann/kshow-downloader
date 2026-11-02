"""Migration of a deployed schema-v1 database (one show = one season) to schema v2.

The v1 fixture is hand-written from the models as of 15c4ff8, in the shape the production
database actually has: tables created by create_all, then poster_url / torrent_missing_since /
missing_since appended later by add_missing_columns (so they sit at the end of the table).
"""

import sqlite3
from datetime import date, timedelta
from pathlib import Path

import pytest
from sqlalchemy import create_engine, event, select

from kshow import migrations
from kshow.db import Episode, Grab, Season, Setting, Show, init_db

V1_DDL = [
    """CREATE TABLE shows (
	id INTEGER NOT NULL,
	search_title VARCHAR NOT NULL,
	release_season INTEGER NOT NULL,
	tvdb_id INTEGER,
	tvdb_season INTEGER NOT NULL,
	folder_name VARCHAR NOT NULL,
	season_folder VARCHAR NOT NULL,
	status VARCHAR NOT NULL,
	season_first_aired DATE,
	added_at DATETIME NOT NULL,
	PRIMARY KEY (id)
)""",
    """CREATE TABLE sources (
	id INTEGER NOT NULL,
	name VARCHAR NOT NULL,
	kind VARCHAR NOT NULL,
	domain_template VARCHAR NOT NULL,
	current_url VARCHAR NOT NULL,
	priority INTEGER NOT NULL,
	enabled BOOLEAN NOT NULL,
	unreachable_since DATETIME,
	PRIMARY KEY (id)
)""",
    """CREATE TABLE settings (
	"key" VARCHAR NOT NULL,
	value VARCHAR NOT NULL,
	PRIMARY KEY ("key")
)""",
    """CREATE TABLE episodes (
	id INTEGER NOT NULL,
	show_id INTEGER NOT NULL,
	number INTEGER NOT NULL,
	tvdb_airdate DATE,
	state VARCHAR NOT NULL,
	filed_path VARCHAR,
	overdue_notified BOOLEAN NOT NULL,
	PRIMARY KEY (id),
	UNIQUE (show_id, number),
	FOREIGN KEY(show_id) REFERENCES shows (id)
)""",
    """CREATE TABLE grabs (
	id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,
	episode_id INTEGER NOT NULL,
	post_url VARCHAR NOT NULL,
	release_name VARCHAR NOT NULL,
	qbit_tag VARCHAR NOT NULL,
	state VARCHAR NOT NULL,
	progress DOUBLE NOT NULL,
	last_progress_at DATETIME NOT NULL,
	created_at DATETIME NOT NULL,
	FOREIGN KEY(episode_id) REFERENCES episodes (id)
)""",
    # Added at runtime by add_missing_columns on later releases.
    "ALTER TABLE shows ADD COLUMN poster_url VARCHAR",
    "ALTER TABLE grabs ADD COLUMN torrent_missing_since DATETIME",
    "ALTER TABLE grabs ADD COLUMN missing_since DATETIME",
]

POSTER = "https://artworks.thetvdb.com/banners/v4/series/468944/posters/x.jpg"
# Episode ids deliberately don't start at 1, so "same ids" is a real check.
EP_ID = {n: 100 + n for n in range(1, 13)}


def make_v1_db(path: Path) -> None:
    con = sqlite3.connect(path)
    for ddl in V1_DDL:
        con.execute(ddl)
    con.execute(
        "INSERT INTO shows (id, search_title, release_season, tvdb_id, tvdb_season, folder_name,"
        " season_folder, status, season_first_aired, added_at, poster_url) VALUES"
        " (1, '신인감독 김연경', 2, 468944, 2, 'The Wonder Coach', 'Season 2', 'following',"
        " '2026-09-28', '2026-10-20 21:30:00.123456', ?)",
        (POSTER,),
    )
    for n in range(1, 13):
        downloaded = n <= 4
        con.execute(
            "INSERT INTO episodes (id, show_id, number, tvdb_airdate, state, filed_path,"
            " overdue_notified) VALUES (?, 1, ?, ?, ?, ?, ?)",
            (
                EP_ID[n],
                n,
                (date(2026, 9, 28) + timedelta(weeks=n - 1)).isoformat(),
                "downloaded" if downloaded else "wanted",
                f"/tv/The Wonder Coach/Season 2/The Wonder Coach - S02E{n:02}.mkv"
                if downloaded
                else None,
                1 if n == 5 else 0,
            ),
        )
    grabs = [
        (5, EP_ID[3], "done", "kshow-5"),
        (7, EP_ID[4], "add_failed", "kshow-7"),
        (9, EP_ID[4], "failed", "kshow-9"),
    ]
    for gid, ep, state, tag in grabs:
        con.execute(
            "INSERT INTO grabs (id, episode_id, post_url, release_name, qbit_tag, state, progress,"
            " last_progress_at, created_at) VALUES (?, ?, 'https://t/p', 'rel', ?, ?, 0.5,"
            " '2026-10-21 10:00:00', '2026-10-21 09:00:00')",
            (gid, ep, tag, state),
        )
    con.execute("DELETE FROM grabs WHERE id = 9")  # sqlite_sequence stays at 9
    con.execute("INSERT INTO settings VALUES ('stall_hours', '12')")
    con.execute(
        "INSERT INTO sources VALUES (1, 'torrenttip', 'torrenttip',"
        " 'https://torrenttip{N}.top', 'https://torrenttip251.top', 1, 1, NULL)"
    )
    con.commit()
    con.close()


def sql(path: Path, query: str) -> list[tuple]:
    con = sqlite3.connect(path)
    try:
        return con.execute(query).fetchall()
    finally:
        con.close()


def dump(path: Path) -> list[str]:
    con = sqlite3.connect(path)
    try:
        return list(con.iterdump())
    finally:
        con.close()


def backups(path: Path) -> list[Path]:
    return sorted(path.parent.glob(f"{path.name}.bak-*"))


@pytest.fixture
def v1(tmp_path) -> Path:
    path = tmp_path / "kshow.db"
    make_v1_db(path)
    return path


def test_migrates_wonder_coach_losslessly(v1):
    Session = init_db(f"sqlite:///{v1}")
    with Session() as s:
        [show] = s.scalars(select(Show)).all()
        assert show.id == 1
        assert show.search_title == "신인감독 김연경"
        assert show.tvdb_id == 468944
        assert show.folder_name == "The Wonder Coach"
        assert show.status == "following"
        assert show.poster_url == POSTER
        assert show.added_at.isoformat() == "2026-10-20T21:30:00.123456"
        assert show.max_known_season == 2

        [season] = s.scalars(select(Season)).all()
        assert show.seasons == [season] and season.show is show
        assert season.number == 2
        assert season.release_season == 2
        assert season.folder == "Season 2"
        assert season.followed is True
        assert season.followed_since == date(2026, 10, 20)
        assert season.first_aired == date(2026, 9, 28)

        eps = s.scalars(select(Episode).order_by(Episode.number)).all()
        assert [e.number for e in eps] == list(range(1, 13))
        assert [e.id for e in eps] == [EP_ID[n] for n in range(1, 13)]
        assert all(e.season is season for e in eps)
        for e in eps[:4]:
            assert (e.state, e.monitored) == ("downloaded", False)
            assert e.filed_path.endswith(f"S02E{e.number:02}.mkv")
        for e in eps[4:]:
            assert (e.state, e.monitored, e.filed_path) == ("wanted", True, None)
        assert eps[4].overdue_notified is True and eps[5].overdue_notified is False
        assert eps[0].tvdb_airdate == date(2026, 9, 28)
        assert eps[11].tvdb_airdate == date(2026, 12, 14)

        grabs = s.scalars(select(Grab).order_by(Grab.id)).all()
        assert [(g.id, g.episode_id, g.qbit_tag, g.state) for g in grabs] == [
            (5, EP_ID[3], "kshow-5", "done"),
            (7, EP_ID[4], "kshow-7", "add_failed"),
        ]
        assert s.get(Setting, "stall_hours").value == "12"

        # The AUTOINCREMENT counter survived: a deleted grab id is never handed out again.
        new = Grab(episode_id=EP_ID[6], post_url="u", release_name="r", qbit_tag="")
        s.add(new)
        s.commit()
        assert new.id == 10

    assert sql(v1, "PRAGMA user_version") == [(2,)]
    assert sql(v1, "PRAGMA foreign_key_check") == []
    tables = {r[0] for r in sql(v1, "SELECT name FROM sqlite_master WHERE type='table'")}
    assert not {"shows_v1", "episodes_v1"} & tables
    assert {"shows", "seasons", "episodes", "grabs", "settings", "sources"} <= tables


def test_migrates_episode_states_to_monitored(tmp_path):
    path = tmp_path / "kshow.db"
    make_v1_db(path)
    con = sqlite3.connect(path)
    con.execute(f"UPDATE episodes SET state = 'downloading' WHERE id = {EP_ID[5]}")
    con.execute(f"UPDATE episodes SET state = 'failed' WHERE id = {EP_ID[6]}")
    con.execute("UPDATE shows SET status = 'stopped'")
    con.commit()
    con.close()
    Session = init_db(f"sqlite:///{path}")
    with Session() as s:
        assert s.get(Episode, EP_ID[5]).monitored is True
        assert s.get(Episode, EP_ID[6]).monitored is True
        assert s.get(Episode, EP_ID[1]).monitored is False
        [season] = s.scalars(select(Season)).all()
        # Followed, so the show page offers Follow again; a stopped show is never checked.
        assert season.followed is True
        assert season.followed_since == date(2026, 10, 20)
        assert season.show.status == "stopped"


def test_backup_file_written(v1):
    before = dump(v1)
    init_db(f"sqlite:///{v1}")
    [bak] = backups(v1)
    assert bak.name.startswith("kshow.db.bak-") and len(bak.name) == len("kshow.db.bak-") + 14
    assert dump(bak) == before
    cols = [r[1] for r in sql(bak, "PRAGMA table_info(shows)")]
    assert "tvdb_season" in cols and "season_folder" in cols
    assert sql(bak, "SELECT count(*) FROM episodes") == [(12,)]


def test_grabs_fk_still_references_episodes(v1):
    init_db(f"sqlite:///{v1}")
    [(ddl,)] = sql(v1, "SELECT sql FROM sqlite_master WHERE name = 'grabs'")
    assert "REFERENCES episodes" in ddl
    assert "episodes_v1" not in ddl
    assert "AUTOINCREMENT" in ddl
    assert sql(v1, "SELECT seq FROM sqlite_sequence WHERE name = 'grabs'") == [(9,)]
    for (ddl,) in sql(v1, "SELECT sql FROM sqlite_master WHERE sql IS NOT NULL"):
        assert "_v1" not in ddl


def test_migration_is_idempotent(v1):
    url = f"sqlite:///{v1}"
    init_db(url)
    dump = sql(v1, "SELECT type, name, sql FROM sqlite_master ORDER BY name")
    rows = {t: sql(v1, f"SELECT * FROM {t} ORDER BY 1") for t in ("shows", "seasons", "episodes")}
    init_db(url)
    assert len(backups(v1)) == 1
    assert sql(v1, "SELECT type, name, sql FROM sqlite_master ORDER BY name") == dump
    for t, before in rows.items():
        assert sql(v1, f"SELECT * FROM {t} ORDER BY 1") == before


def test_fresh_db_is_v2(tmp_path):
    path = tmp_path / "kshow.db"
    Session = init_db(f"sqlite:///{path}")
    assert sql(path, "PRAGMA user_version") == [(2,)]
    tables = {r[0] for r in sql(path, "SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"shows", "seasons", "episodes", "grabs"} <= tables
    assert backups(path) == []
    with Session() as s:
        assert s.scalars(select(Show)).all() == []


def test_fresh_in_memory_db_is_v2():
    Session = init_db("sqlite://")
    with Session() as s:
        assert s.connection().exec_driver_sql("PRAGMA user_version").scalar() == 2


def test_migration_with_foreign_keys_enabled_on_the_connection(v1):
    """PRAGMA foreign_keys is a no-op inside a transaction; the migration must switch it off
    before it begins one, or renaming episodes would rewrite grabs' FK to episodes_v1."""
    engine = create_engine(f"sqlite:///{v1}")

    @event.listens_for(engine, "connect")
    def fk_on(dbapi_con, _):
        dbapi_con.execute("PRAGMA foreign_keys=ON")

    migrations.migrate(engine, v1)
    [(ddl,)] = sql(v1, "SELECT sql FROM sqlite_master WHERE name = 'grabs'")
    assert "REFERENCES episodes" in ddl and "episodes_v1" not in ddl
    assert sql(v1, "SELECT count(*) FROM episodes") == [(12,)]
    with engine.connect() as conn:
        # The connection's own settings are restored afterwards.
        assert conn.exec_driver_sql("PRAGMA foreign_keys").scalar() == 1
        assert conn.exec_driver_sql("PRAGMA legacy_alter_table").scalar() == 0
    engine.dispose()


def test_failed_migration_rolls_back_and_keeps_backup(v1, monkeypatch):
    before = {t: sql(v1, f"SELECT * FROM {t} ORDER BY 1") for t in ("shows", "episodes", "grabs")}
    schema = sql(v1, "SELECT type, name, sql FROM sqlite_master ORDER BY name")
    real_copy = migrations._copy_v1_rows

    def copy_then_fail(con):
        real_copy(con)
        raise RuntimeError("boom")

    monkeypatch.setattr(migrations, "_copy_v1_rows", copy_then_fail)
    with pytest.raises(RuntimeError, match="boom"):
        init_db(f"sqlite:///{v1}")

    assert sql(v1, "PRAGMA user_version") == [(0,)]
    assert sql(v1, "SELECT type, name, sql FROM sqlite_master ORDER BY name") == schema
    for t, rows in before.items():
        assert sql(v1, f"SELECT * FROM {t} ORDER BY 1") == rows
    assert len(backups(v1)) == 1

    monkeypatch.undo()  # the next start migrates normally
    Session = init_db(f"sqlite:///{v1}")
    with Session() as s:
        assert len(s.scalars(select(Episode)).all()) == 12
    assert sql(v1, "PRAGMA user_version") == [(2,)]


def test_lost_rows_abort_the_migration(v1):
    """An episode whose show is gone can't be placed in a season: refuse rather than drop it."""
    con = sqlite3.connect(v1)
    con.execute("INSERT INTO episodes VALUES (999, 42, 1, NULL, 'wanted', NULL, 0)")
    con.commit()
    con.close()
    with pytest.raises(RuntimeError, match="episodes"):
        init_db(f"sqlite:///{v1}")
    assert sql(v1, "PRAGMA user_version") == [(0,)]
    assert sql(v1, "SELECT count(*) FROM episodes") == [(13,)]


def test_v1_db_without_runtime_added_columns(tmp_path):
    """A v1 database from before poster_url existed migrates too (poster_url becomes NULL)."""
    path = tmp_path / "kshow.db"
    con = sqlite3.connect(path)
    for ddl in V1_DDL[:5]:
        con.execute(ddl)
    con.execute(
        "INSERT INTO shows VALUES (3, '놀면 뭐하니', 1, NULL, 3, 'Hangout', 'Season 03',"
        " 'finished', NULL, '2026-10-01 10:00:00')"
    )
    con.execute("INSERT INTO episodes VALUES (1, 3, 1, NULL, 'downloaded', '/tv/x.mkv', 0)")
    con.commit()
    con.close()
    Session = init_db(f"sqlite:///{path}")
    with Session() as s:
        show = s.get(Show, 3)
        assert show.poster_url is None and show.tvdb_id is None and show.max_known_season == 3
        [season] = show.seasons
        assert (season.number, season.release_season, season.folder) == (3, 1, "Season 03")
        # A finished show's season stays followed: a new season brings the show back.
        assert season.followed is True and season.first_aired is None
        assert season.followed_since == date(2026, 10, 1)
        assert s.get(Episode, 1).season is season
    grab_cols = [r[1] for r in sql(path, "PRAGMA table_info(grabs)")]
    assert grab_cols.count("missing_since") == 1
