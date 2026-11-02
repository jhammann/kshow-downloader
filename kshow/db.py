from datetime import date, datetime, timezone
from pathlib import Path

from sqlalchemy import ForeignKey, UniqueConstraint, create_engine, inspect, select, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    Session,
    mapped_column,
    relationship,
    sessionmaker,
)
from sqlalchemy.pool import StaticPool


def utcnow() -> datetime:
    """Naive UTC now; the app-wide convention for all timestamps."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Base(DeclarativeBase):
    pass


class Show(Base):
    __tablename__ = "shows"

    id: Mapped[int] = mapped_column(primary_key=True)
    search_title: Mapped[str]
    tvdb_id: Mapped[int | None]
    folder_name: Mapped[str]
    status: Mapped[str] = mapped_column(default="following")  # following | finished | stopped
    poster_url: Mapped[str | None]  # https:// TVDB artwork, or None for a placeholder
    added_at: Mapped[datetime] = mapped_column(default=utcnow)
    # Highest season number known when the show was added (or migrated, or last synced). A
    # TVDB season above it is "new" and gets followed automatically; lower ones never are.
    max_known_season: Mapped[int] = mapped_column(default=0)

    seasons: Mapped[list["Season"]] = relationship(
        back_populates="show", order_by="Season.number"
    )


class Season(Base):
    __tablename__ = "seasons"
    __table_args__ = (UniqueConstraint("show_id", "number"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    show_id: Mapped[int] = mapped_column(ForeignKey("shows.id"))
    number: Mapped[int]  # TVDB season number
    release_season: Mapped[int]  # season number as it appears in release names
    folder: Mapped[str]  # season folder under the show folder, e.g. "Season 2" / "Season 01"
    followed: Mapped[bool] = mapped_column(default=False)
    followed_since: Mapped[date | None]  # Korean date; episodes airing from then are monitored
    first_aired: Mapped[date | None]

    show: Mapped[Show] = relationship(back_populates="seasons")
    episodes: Mapped[list["Episode"]] = relationship(
        back_populates="season", order_by="Episode.number"
    )


class Episode(Base):
    __tablename__ = "episodes"
    __table_args__ = (UniqueConstraint("season_id", "number"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    season_id: Mapped[int] = mapped_column(ForeignKey("seasons.id"))
    number: Mapped[int]
    tvdb_airdate: Mapped[date | None]
    state: Mapped[str] = mapped_column(default="wanted")  # wanted | downloading | downloaded | failed
    filed_path: Mapped[str | None]
    overdue_notified: Mapped[bool] = mapped_column(default=False)
    # Fetched automatically when wanted. Manual searches never set this.
    monitored: Mapped[bool] = mapped_column(default=False)

    season: Mapped[Season] = relationship(back_populates="episodes")


class Grab(Base):
    __tablename__ = "grabs"
    __table_args__ = {"sqlite_autoincrement": True}  # ids become qBittorrent tags; never reuse

    id: Mapped[int] = mapped_column(primary_key=True)
    episode_id: Mapped[int] = mapped_column(ForeignKey("episodes.id"))
    post_url: Mapped[str]
    release_name: Mapped[str]
    qbit_tag: Mapped[str]
    state: Mapped[str] = mapped_column(default="downloading")  # downloading | done | failed
    progress: Mapped[float] = mapped_column(default=0)
    last_progress_at: Mapped[datetime] = mapped_column(default=utcnow)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    # First poll at which the torrent was absent from qBittorrent's category list.
    torrent_missing_since: Mapped[datetime | None]
    # First poll at which a seeding torrent's file was missing on disk.
    missing_since: Mapped[datetime | None]


class SourceRow(Base):
    __tablename__ = "sources"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str]
    kind: Mapped[str] = mapped_column(default="torrenttip")
    domain_template: Mapped[str]
    current_url: Mapped[str]
    priority: Mapped[int]
    enabled: Mapped[bool] = mapped_column(default=True)
    unreachable_since: Mapped[datetime | None]


class Setting(Base):
    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(primary_key=True)
    value: Mapped[str]


DEFAULT_SETTINGS = {
    "check_interval_minutes": "20",
    "qbit_category": "kshow",
    "qbit_save_path": "/downloads/kshow",
    "preferred_resolution": "720p",
    "stall_hours": "6",
    "path_map": "",
    "qbit_outage_since": "",  # naive-UTC ISO time qBittorrent started failing, "" when fine
}


def get_setting(session: Session, key: str) -> str:
    row = session.get(Setting, key)
    return row.value if row else DEFAULT_SETTINGS[key]


def set_setting(session: Session, key: str, value: str) -> None:
    row = session.get(Setting, key)
    if row:
        row.value = value
    else:
        session.add(Setting(key=key, value=value))
    session.commit()


def add_missing_columns(engine: Engine) -> None:
    """Add nullable model columns that an existing table lacks.

    create_all creates missing tables but never alters existing ones, so a column added to a
    model later would be missing from a deployed database. Only nullable columns are added:
    existing rows get NULL, which keeps their data valid. Safe to run on every start.
    """
    insp = inspect(engine)
    preparer = engine.dialect.identifier_preparer
    with engine.begin() as conn:
        for table in Base.metadata.sorted_tables:
            if not insp.has_table(table.name):
                continue
            existing = {c["name"] for c in insp.get_columns(table.name)}
            for col in table.columns:
                if col.name in existing or not col.nullable:
                    continue
                ddl = col.type.compile(dialect=engine.dialect)
                conn.execute(
                    text(
                        f"ALTER TABLE {preparer.quote(table.name)} "
                        f"ADD COLUMN {preparer.quote(col.name)} {ddl}"
                    )
                )


def init_db(url: str) -> sessionmaker:
    from kshow.migrations import migrate  # imports this module's models

    kwargs = {}
    if url in ("sqlite://", "sqlite:///:memory:"):
        kwargs = {"poolclass": StaticPool, "connect_args": {"check_same_thread": False}}
    engine = create_engine(url, **kwargs)
    db_file = engine.url.database
    migrate(engine, Path(db_file) if db_file and db_file != ":memory:" else None)
    Base.metadata.create_all(engine)
    add_missing_columns(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    with factory() as s:
        for key, value in DEFAULT_SETTINGS.items():
            if s.get(Setting, key) is None:
                s.add(Setting(key=key, value=value))
        if s.scalar(select(SourceRow).where(SourceRow.name == "torrenttip")) is None:
            s.add(
                SourceRow(
                    name="torrenttip",
                    domain_template="https://torrenttip{N}.top",
                    current_url="https://torrenttip250.top",
                    priority=1,
                )
            )
        s.commit()
    return factory
