from datetime import datetime

from pydantic import BaseModel, ConfigDict


class RootFolderOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    path: str


class RootFolderIn(BaseModel):
    path: str


class SourceLinkOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    source_name: str
    external_id: str
    external_title: str
    external_url: str


class ChapterOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    number: float
    volume: int | None
    title: str
    monitored: bool
    downloaded: bool
    file_path: str


class SeriesOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    anilist_id: int | None
    title: str
    description: str
    status: str
    year: int | None
    cover_url: str
    banner_url: str
    genres: str
    monitored: bool
    root_folder_id: int | None
    folder_name: str
    total_chapters: int | None
    total_volumes: int | None
    added_at: datetime
    chapter_count: int = 0
    downloaded_count: int = 0


class SeriesDetailOut(SeriesOut):
    chapters: list[ChapterOut] = []
    source_links: list[SourceLinkOut] = []


class AddSeriesIn(BaseModel):
    anilist_id: int
    root_folder_id: int
    monitored: bool = True
    search_now: bool = False


class SeriesUpdateIn(BaseModel):
    monitored: bool | None = None
    root_folder_id: int | None = None
    folder_name: str | None = None


class ChapterMonitorIn(BaseModel):
    chapter_ids: list[int]
    monitored: bool


class MetadataResult(BaseModel):
    provider: str
    provider_id: str
    title: str
    alt_titles: list[str]
    description: str
    status: str
    year: int | None
    cover_url: str
    genres: list[str]
    total_chapters: int | None
    total_volumes: int | None
    in_library: bool = False


class ReleaseOut(BaseModel):
    """Interactive-search result: either a direct source chapter or a torrent."""
    kind: str  # direct | torrent
    source_name: str
    title: str
    chapter_number: float | None = None
    external_id: str = ""  # direct: source chapter id
    url: str = ""
    magnet: str = ""
    size_bytes: int = 0
    seeders: int = 0
    leechers: int = 0


class GrabIn(BaseModel):
    # direct grab
    chapter_id: int | None = None
    source_name: str | None = None
    external_id: str | None = None
    # torrent grab
    series_id: int | None = None
    magnet: str | None = None
    title: str | None = None


class QueueItemOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    series_id: int | None
    chapter_id: int | None
    kind: str
    status: str
    title: str
    source_name: str
    progress: float
    error: str
    created_at: datetime
    series_title: str = ""


class HistoryOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    series_id: int | None
    event: str
    detail: str
    source_name: str
    created_at: datetime
    series_title: str = ""


class WantedItemOut(BaseModel):
    chapter_id: int
    series_id: int
    series_title: str
    cover_url: str
    number: float
    volume: int | None
    title: str


class QbtTestIn(BaseModel):
    url: str
    username: str
    password: str


class SystemStatus(BaseModel):
    version: str
    series_count: int
    chapter_count: int
    downloaded_count: int
    queue_count: int


# ---- library import / scan / rename ----

class ScanResultOut(BaseModel):
    folder: str
    folder_exists: bool
    matched_chapters: int
    volume_files: int
    cleared: int
    unmatched: list[str] = []


class RenameItemOut(BaseModel):
    chapter_ids: list[int]
    current_path: str
    current_name: str
    new_path: str
    new_name: str


class RenameApplyIn(BaseModel):
    # optional subset; when omitted, apply all currently-planned renames
    chapter_ids: list[int] | None = None


class RenameOutcomeOut(BaseModel):
    current_name: str
    new_name: str
    status: str
    detail: str = ""


class SeriesFileOut(BaseModel):
    path: str
    name: str
    is_dir: bool
    chapter_number: float | None = None
    volume_number: int | None = None
    matched_chapter_id: int | None = None


class FileMapIn(BaseModel):
    file_path: str
    chapter_id: int


class FileMapRangeIn(BaseModel):
    file_path: str
    from_number: float
    to_number: float


class FileMapRangeOut(BaseModel):
    mapped: int
    volume: int | None


class SourceCandidateOut(BaseModel):
    source_name: str
    external_id: str
    title: str
    url: str = ""
    alt_titles: list[str] = []


class SourceLinkIn(BaseModel):
    source_name: str
    external_id: str
    external_title: str = ""
    external_url: str = ""


class ResyncOut(BaseModel):
    chapters: int
    matched_chapters: int


class SeriesFolderOut(BaseModel):
    id: int | None  # None for the primary folder
    path: str
    resolved: str
    primary: bool
    exists: bool


class SeriesFolderIn(BaseModel):
    path: str


class FilesystemEntryOut(BaseModel):
    name: str
    path: str


class FilesystemListOut(BaseModel):
    path: str
    parent: str | None
    entries: list[FilesystemEntryOut]
