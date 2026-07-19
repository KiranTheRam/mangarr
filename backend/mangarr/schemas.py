from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


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
    title_source: str
    volume_source: str
    title_locked: bool
    volume_locked: bool
    monitored: bool
    downloaded: bool
    file_path: str
    available_sources: str


class SeriesOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    anilist_id: int | None
    mangaupdates_id: int | None
    title: str
    english_title: str = ""
    alt_titles: str = ""  # newline-joined; lets the client match any known name
    description: str
    status: str
    year: int | None
    cover_url: str
    banner_url: str
    genres: str
    monitored: bool
    root_folder_id: int | None
    folder_name: str
    folder_pinned: bool
    total_chapters: int | None
    total_volumes: int | None
    added_at: datetime
    # main (whole-numbered) chapters only — these define "fully downloaded"
    chapter_count: int = 0
    downloaded_count: int = 0
    # decimal chapters (60.5 …): searched for like any other, but excluded
    # from the counts above so a missing special never blocks completion
    special_count: int = 0
    special_downloaded_count: int = 0


class SeriesDetailOut(SeriesOut):
    chapters: list[ChapterOut] = []
    source_links: list[SourceLinkOut] = []
    refreshing: bool = False  # a full refresh is running in the background


class AddSeriesIn(BaseModel):
    # exactly one of the provider ids (MangaUpdates is the primary provider)
    mangaupdates_id: int | None = None
    anilist_id: int | None = None
    root_folder_id: int
    monitored: bool = True
    search_now: bool = False
    english_title: str = ""
    alt_titles: list[str] = Field(default_factory=list)
    # series folder under the root; empty means derive from the title
    folder_name: str = ""
    # the user picked the folder deliberately — scans must not re-adopt a
    # title-matching existing folder over it
    folder_pinned: bool = False
    extra_folders: list[str] = Field(default_factory=list)


class FolderPreviewIn(BaseModel):
    """Ask which folder a prospective series would use before adding it."""
    root_folder_id: int
    title: str
    alt_titles: list[str] = Field(default_factory=list)


class FolderPreviewOut(BaseModel):
    folder_name: str
    path: str
    exists: bool
    matched: bool  # an existing folder was adopted (vs a fresh default name)
    # the title-derived name a fresh folder would get, so the UI can offer
    # "create a new folder instead" when the match is wrong
    default_folder_name: str


class SeriesUpdateIn(BaseModel):
    monitored: bool | None = None
    root_folder_id: int | None = None
    folder_name: str | None = None
    # None + a folder_name update pins implicitly (an explicit folder edit is
    # an explicit choice); pass False to re-enable folder adoption
    folder_pinned: bool | None = None


class ChapterMonitorIn(BaseModel):
    chapter_ids: list[int]
    monitored: bool


class ChapterMetadataIn(BaseModel):
    """User-confirmed chapter metadata. Locks survive future refreshes."""
    title: str = ""
    volume: int | None = Field(default=None, ge=1)
    title_locked: bool = True
    volume_locked: bool = True


class MetadataResult(BaseModel):
    provider: str
    provider_id: str
    title: str
    english_title: str = ""
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
    chapter_id: int | None = None
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


class QueueRemoveIn(BaseModel):
    ids: list[int]


class QueueRemoveOut(BaseModel):
    removed: int


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


class ApiKeyOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    name: str
    key: str
    created_at: datetime
    last_used_at: datetime | None


class ApiKeyCreateIn(BaseModel):
    name: str


class QbtTestIn(BaseModel):
    url: str
    username: str
    password: str


class WebhookTestIn(BaseModel):
    url: str
    secret: str = ""


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
    conflict: bool = False


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
    covered_count: int = 0  # chapters this file covers (N for a volume archive)


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


class CleanupFileOut(BaseModel):
    path: str
    name: str
    size: int
    referenced: bool
    keep: bool


class CleanupGroupOut(BaseModel):
    label: str
    files: list[CleanupFileOut]


class CleanupPlanOut(BaseModel):
    groups: list[CleanupGroupOut] = []
    orphans: list[CleanupFileOut] = []


class CleanupApplyIn(BaseModel):
    delete: list[str]


class CleanupResultOut(BaseModel):
    deleted: int
    repointed: int
    skipped: int
    freed_bytes: int


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


class VolumeResyncIn(BaseModel):
    """Optional body: apply a specific source's volume map (as offered by the
    resync preview) instead of the auto-selected most complete one."""
    source: str | None = None


class VolumeResyncOut(BaseModel):
    has_data: bool  # False when no linked source provides volume data
    assigned: int  # chapters with a volume after the resync
    changed: int  # chapters whose volume assignment changed
    repointed: int  # chapters re-covered by a different file on disk
    cleared: int  # chapters no longer backed by any file


class VolumeDiffRowOut(BaseModel):
    number: float
    old_volume: int | None
    new_volume: int | None


class VolumeMappingRowOut(BaseModel):
    """One chapter's volume assignment as it would stand after the resync."""
    number: float
    volume: int | None


class VolumeCandidateOut(BaseModel):
    """Dry-run outcome of applying one source's volume map."""
    source: str
    map_size: int  # sanitized chapter→volume entries — the ranking key
    assigned: int
    changed: int
    repointed: int
    cleared: int
    has_changes: bool  # would touch assignments or file coverage
    diff: list[VolumeDiffRowOut]
    # the complete resulting chapter→volume picture (every chapter, in
    # order), so the preview can show the whole mapping, not just the diff
    mapping: list[VolumeMappingRowOut]


class VolumeResyncPreviewOut(BaseModel):
    # ranked like the resync itself ranks maps: most complete first, so the
    # first candidate is what an unqualified resync would apply
    candidates: list[VolumeCandidateOut]


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
