from __future__ import annotations

import enum
from datetime import datetime, timezone

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class SeriesStatus(str, enum.Enum):
    RELEASING = "releasing"
    FINISHED = "finished"
    HIATUS = "hiatus"
    CANCELLED = "cancelled"
    NOT_YET_RELEASED = "not_yet_released"
    UNKNOWN = "unknown"


class RootFolder(Base):
    __tablename__ = "root_folders"

    id: Mapped[int] = mapped_column(primary_key=True)
    path: Mapped[str] = mapped_column(String, unique=True)

    series: Mapped[list[Series]] = relationship(back_populates="root_folder")


class Series(Base):
    __tablename__ = "series"

    id: Mapped[int] = mapped_column(primary_key=True)
    anilist_id: Mapped[int | None] = mapped_column(Integer, unique=True, nullable=True)
    mangaupdates_id: Mapped[int | None] = mapped_column(BigInteger, unique=True, nullable=True)
    title: Mapped[str] = mapped_column(String)
    sort_title: Mapped[str] = mapped_column(String, default="")
    alt_titles: Mapped[str] = mapped_column(Text, default="")  # newline-separated
    description: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[SeriesStatus] = mapped_column(
        Enum(SeriesStatus, values_callable=lambda e: [m.value for m in e]),
        default=SeriesStatus.UNKNOWN,
    )
    year: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cover_url: Mapped[str] = mapped_column(String, default="")
    banner_url: Mapped[str] = mapped_column(String, default="")
    genres: Mapped[str] = mapped_column(String, default="")  # comma-separated
    total_chapters: Mapped[int | None] = mapped_column(Integer, nullable=True)
    total_volumes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    monitored: Mapped[bool] = mapped_column(Boolean, default=True)
    root_folder_id: Mapped[int | None] = mapped_column(ForeignKey("root_folders.id"), nullable=True)
    folder_name: Mapped[str] = mapped_column(String, default="")
    # the folder was chosen explicitly by the user — scans must not re-adopt
    # a title-matching existing folder over it
    folder_pinned: Mapped[bool] = mapped_column(Boolean, default=False)
    added_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    # when provider metadata (status, totals, …) was last pulled; the monitor
    # re-refreshes stale series so finished/hiatus states don't rot
    metadata_refreshed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    root_folder: Mapped[RootFolder | None] = relationship(back_populates="series")
    chapters: Mapped[list[Chapter]] = relationship(
        back_populates="series", cascade="all, delete-orphan", order_by="Chapter.number"
    )
    source_links: Mapped[list[SeriesSourceLink]] = relationship(
        back_populates="series", cascade="all, delete-orphan"
    )
    extra_folders: Mapped[list[SeriesFolder]] = relationship(
        back_populates="series", cascade="all, delete-orphan"
    )


class SeriesFolder(Base):
    """Additional library directories scanned for a series, beyond its primary
    folder (Series.folder_name). Lets one series span, e.g., a volumes folder
    and a separate loose-chapters folder."""

    __tablename__ = "series_folders"
    __table_args__ = (UniqueConstraint("series_id", "path"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    series_id: Mapped[int] = mapped_column(ForeignKey("series.id"))
    path: Mapped[str] = mapped_column(String)  # relative to root when under it, else absolute

    series: Mapped[Series] = relationship(back_populates="extra_folders")


class SeriesSourceLink(Base):
    """Maps a series to its identifier on a content source (e.g. mangadex UUID,
    weebcentral slug). New sources need no schema change."""

    __tablename__ = "series_source_links"
    __table_args__ = (UniqueConstraint("series_id", "source_name"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    series_id: Mapped[int] = mapped_column(ForeignKey("series.id"))
    source_name: Mapped[str] = mapped_column(String)
    external_id: Mapped[str] = mapped_column(String)
    external_title: Mapped[str] = mapped_column(String, default="")
    external_url: Mapped[str] = mapped_column(String, default="")

    series: Mapped[Series] = relationship(back_populates="source_links")


class Chapter(Base):
    __tablename__ = "chapters"
    __table_args__ = (UniqueConstraint("series_id", "number"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    series_id: Mapped[int] = mapped_column(ForeignKey("series.id"))
    number: Mapped[float] = mapped_column(Float)  # 10.5 etc.
    volume: Mapped[int | None] = mapped_column(Integer, nullable=True)
    title: Mapped[str] = mapped_column(String, default="")
    # Provenance is tracked per field because chapter existence, title, and
    # printed-volume placement often come from different providers.
    title_source: Mapped[str] = mapped_column(String, default="")
    volume_source: Mapped[str] = mapped_column(String, default="")
    title_locked: Mapped[bool] = mapped_column(Boolean, default=False)
    volume_locked: Mapped[bool] = mapped_column(Boolean, default=False)
    monitored: Mapped[bool] = mapped_column(Boolean, default=True)
    downloaded: Mapped[bool] = mapped_column(Boolean, default=False)
    file_path: Mapped[str] = mapped_column(String, default="")
    released_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Comma-separated enabled direct sources whose latest successful chapter
    # listing contains this exact number.  Metadata-only/catalogue rows remain
    # visible while the UI can honestly say that no linked source serves them.
    available_sources: Mapped[str | None] = mapped_column(
        String, nullable=True, default=None
    )

    series: Mapped[Series] = relationship(back_populates="chapters")


class DownloadKind(str, enum.Enum):
    DIRECT = "direct"
    TORRENT = "torrent"


class DownloadStatus(str, enum.Enum):
    QUEUED = "queued"
    DOWNLOADING = "downloading"
    IMPORTING = "importing"
    DONE = "done"
    FAILED = "failed"


class Download(Base):
    __tablename__ = "downloads"

    id: Mapped[int] = mapped_column(primary_key=True)
    series_id: Mapped[int | None] = mapped_column(ForeignKey("series.id"), nullable=True)
    chapter_id: Mapped[int | None] = mapped_column(ForeignKey("chapters.id"), nullable=True)
    kind: Mapped[DownloadKind] = mapped_column(
        Enum(DownloadKind, values_callable=lambda e: [m.value for m in e])
    )
    status: Mapped[DownloadStatus] = mapped_column(
        Enum(DownloadStatus, values_callable=lambda e: [m.value for m in e]),
        default=DownloadStatus.QUEUED,
    )
    title: Mapped[str] = mapped_column(String, default="")  # human-readable release title
    source_name: Mapped[str] = mapped_column(String, default="")
    payload: Mapped[str] = mapped_column(Text, default="")  # source-specific: chapter external id / magnet
    torrent_hash: Mapped[str] = mapped_column(String, default="")
    progress: Mapped[float] = mapped_column(Float, default=0.0)  # 0..1
    error: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    series: Mapped[Series | None] = relationship()
    chapter: Mapped[Chapter | None] = relationship()


class HistoryEvent(Base):
    __tablename__ = "history"

    id: Mapped[int] = mapped_column(primary_key=True)
    series_id: Mapped[int | None] = mapped_column(ForeignKey("series.id"), nullable=True)
    chapter_id: Mapped[int | None] = mapped_column(ForeignKey("chapters.id"), nullable=True)
    event: Mapped[str] = mapped_column(String)  # grabbed / imported / failed / deleted
    detail: Mapped[str] = mapped_column(Text, default="")
    source_name: Mapped[str] = mapped_column(String, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    series: Mapped[Series | None] = relationship()
    chapter: Mapped[Chapter | None] = relationship()


class Setting(Base):
    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String, primary_key=True)
    value: Mapped[str] = mapped_column(Text, default="")


class ApiKey(Base):
    """A named API key for scripted/external access (e.g. NextPanel). The web
    UI uses the bootstrap key from initialize.json; these are user-managed keys
    created and revoked from Settings, any of which authenticates API calls."""

    __tablename__ = "api_keys"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String)
    key: Mapped[str] = mapped_column(String, unique=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
