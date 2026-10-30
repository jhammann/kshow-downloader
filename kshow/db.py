from datetime import date, datetime

from sqlalchemy import ForeignKey, UniqueConstraint, create_engine, func, select
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    Session,
    mapped_column,
    sessionmaker,
)
from sqlalchemy.pool import StaticPool


class Base(DeclarativeBase):
    pass


class Show(Base):
    __tablename__ = "shows"

    id: Mapped[int] = mapped_column(primary_key=True)
    search_title: Mapped[str]
    release_season: Mapped[int]
    tvdb_id: Mapped[int | None]
    tvdb_season: Mapped[int]
    folder_name: Mapped[str]
    season_folder: Mapped[str]
    status: Mapped[str] = mapped_column(default="following")  # following | finished | stopped
    season_first_aired: Mapped[date | None]
    added_at: Mapped[datetime] = mapped_column(server_default=func.now())


class Episode(Base):
    __tablename__ = "episodes"
    __table_args__ = (UniqueConstraint("show_id", "number"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    show_id: Mapped[int] = mapped_column(ForeignKey("shows.id"))
    number: Mapped[int]
    tvdb_airdate: Mapped[date | None]
    state: Mapped[str] = mapped_column(default="wanted")  # wanted | downloading | downloaded | failed
    filed_path: Mapped[str | None]
    overdue_notified: Mapped[bool] = mapped_column(default=False)


class Grab(Base):
    __tablename__ = "grabs"

    id: Mapped[int] = mapped_column(primary_key=True)
    episode_id: Mapped[int] = mapped_column(ForeignKey("episodes.id"))
    post_url: Mapped[str]
    release_name: Mapped[str]
    qbit_tag: Mapped[str]
    state: Mapped[str] = mapped_column(default="downloading")  # downloading | done | failed
    progress: Mapped[float] = mapped_column(default=0)
    last_progress_at: Mapped[datetime] = mapped_column(server_default=func.now())
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


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


def init_db(url: str) -> sessionmaker:
    kwargs = {}
    if url in ("sqlite://", "sqlite:///:memory:"):
        kwargs = {"poolclass": StaticPool, "connect_args": {"check_same_thread": False}}
    engine = create_engine(url, **kwargs)
    Base.metadata.create_all(engine)
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
