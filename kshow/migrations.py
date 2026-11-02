"""Schema migrations, keyed on SQLite's PRAGMA user_version.

v1 (user_version 0): one show = one season; season columns on `shows`, episodes keyed by show.
v2: shows -> seasons -> episodes, with a `monitored` flag per episode.
"""

import logging
import sqlite3
from pathlib import Path

from sqlalchemy.engine import Engine
from sqlalchemy.schema import CreateIndex, CreateTable

from kshow.db import Base, utcnow

log = logging.getLogger(__name__)

SCHEMA_VERSION = 2


def migrate(engine: Engine, db_path: Path | None) -> None:
    """Bring the database to SCHEMA_VERSION. Call before create_all.

    A fresh database is created at the current version. A v1 database is backed up next to
    db_path (when given) and converted in one transaction; on any error the transaction is
    rolled back and the error re-raised, so startup fails and the old data stays as it was.
    """
    raw = engine.raw_connection()
    try:
        con: sqlite3.Connection = raw.driver_connection
        has_shows = con.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'shows'"
        ).fetchone()
        version = con.execute("PRAGMA user_version").fetchone()[0]
        raw.rollback()  # end the driver's implicit read transaction, if any
    finally:
        raw.close()

    if not has_shows:
        Base.metadata.create_all(engine)
        with engine.begin() as conn:
            conn.exec_driver_sql(f"PRAGMA user_version = {SCHEMA_VERSION}")
        return
    if version >= SCHEMA_VERSION:
        return

    if db_path is not None:
        backup = _backup(db_path)
        log.info("Backed up the database to %s before migrating it to schema v2", backup)
    _migrate_v1_to_v2(engine)
    log.info("Migrated the database to schema v2 (seasons)")


def _backup(db_path: Path) -> Path:
    target = db_path.with_name(f"{db_path.name}.bak-{utcnow():%Y%m%d%H%M%S}")
    src = sqlite3.connect(db_path)
    try:
        dst = sqlite3.connect(target)
        try:
            src.backup(dst)  # consistent copy, also of a hot journal / WAL
        finally:
            dst.close()
    finally:
        src.close()
    return target


def _migrate_v1_to_v2(engine: Engine) -> None:
    raw = engine.raw_connection()
    try:
        con: sqlite3.Connection = raw.driver_connection
        raw.rollback()
        isolation = con.isolation_level
        fk = con.execute("PRAGMA foreign_keys").fetchone()[0]
        legacy = con.execute("PRAGMA legacy_alter_table").fetchone()[0]
        # Explicit BEGIN/COMMIT: the driver must not open or commit transactions on its own.
        con.isolation_level = None
        try:
            # Both are no-ops inside a transaction, so set them before BEGIN and check they
            # took. With legacy_alter_table on and foreign keys off, renaming `episodes` leaves
            # the FOREIGN KEY text in `grabs` pointing at "episodes", i.e. at the new table.
            con.execute("PRAGMA foreign_keys = OFF")
            con.execute("PRAGMA legacy_alter_table = ON")
            if con.execute("PRAGMA foreign_keys").fetchone()[0] != 0:
                raise RuntimeError("could not switch off foreign keys for the migration")
            if con.execute("PRAGMA legacy_alter_table").fetchone()[0] != 1:
                raise RuntimeError("could not switch on legacy_alter_table for the migration")
            con.execute("BEGIN IMMEDIATE")
            try:
                orphan_grabs = _orphan_grabs(con)
                con.execute("ALTER TABLE shows RENAME TO shows_v1")
                con.execute("ALTER TABLE episodes RENAME TO episodes_v1")
                for name in ("shows", "seasons", "episodes"):
                    table = Base.metadata.tables[name]
                    con.execute(str(CreateTable(table).compile(dialect=engine.dialect)))
                    for index in table.indexes:  # none today; keep create_all's behaviour
                        con.execute(str(CreateIndex(index).compile(dialect=engine.dialect)))
                _copy_v1_rows(con)
                _check_counts(con)
                if _orphan_grabs(con) - orphan_grabs:
                    raise RuntimeError("migration would detach grabs from their episodes")
                con.execute("DROP TABLE episodes_v1")
                con.execute("DROP TABLE shows_v1")
                con.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
                con.execute("COMMIT")
            except BaseException:
                if con.in_transaction:
                    con.execute("ROLLBACK")
                raise
        finally:
            con.execute(f"PRAGMA legacy_alter_table = {legacy}")
            con.execute(f"PRAGMA foreign_keys = {fk}")
            con.isolation_level = isolation
    finally:
        raw.close()


def _columns(con: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in con.execute(f"PRAGMA table_info({table})")}


def _col(present: set[str], name: str) -> str:
    """The v1 column, or NULL for a nullable column an old database never got."""
    return name if name in present else "NULL"


def _copy_v1_rows(con: sqlite3.Connection) -> None:
    shows = _columns(con, "shows_v1")
    episodes = _columns(con, "episodes_v1")
    con.execute(
        "INSERT INTO shows (id, search_title, tvdb_id, folder_name, status, poster_url,"
        " added_at, max_known_season)"
        " SELECT id, search_title, tvdb_id, folder_name, status,"
        f" {_col(shows, 'poster_url')}, added_at, tvdb_season FROM shows_v1"
    )
    # One season per v1 show, with the show's id so episodes can follow by show_id.
    con.execute(
        "INSERT INTO seasons (id, show_id, number, release_season, folder, followed,"
        " followed_since, first_aired)"
        " SELECT id, id, tvdb_season, release_season, season_folder, status = 'following',"
        f" date(added_at), {_col(shows, 'season_first_aired')} FROM shows_v1"
    )
    con.execute(
        "INSERT INTO episodes (id, season_id, number, tvdb_airdate, state, filed_path,"
        " overdue_notified, monitored)"
        f" SELECT e.id, s.id, e.number, {_col(episodes, 'tvdb_airdate')}, e.state,"
        f" {_col(episodes, 'filed_path')}, e.overdue_notified, e.state != 'downloaded'"
        " FROM episodes_v1 e JOIN seasons s ON s.show_id = e.show_id"
    )


def _check_counts(con: sqlite3.Connection) -> None:
    for new, old in (("shows", "shows_v1"), ("seasons", "shows_v1"), ("episodes", "episodes_v1")):
        n_new = con.execute(f"SELECT count(*) FROM {new}").fetchone()[0]
        n_old = con.execute(f"SELECT count(*) FROM {old}").fetchone()[0]
        if n_new != n_old:
            raise RuntimeError(f"migration copied {n_new} {new} rows from {n_old} in {old}")


def _orphan_grabs(con: sqlite3.Connection) -> set[int]:
    if not con.execute("SELECT 1 FROM sqlite_master WHERE name = 'grabs'").fetchone():
        return set()
    return {row[1] for row in con.execute("PRAGMA foreign_key_check(grabs)")}
