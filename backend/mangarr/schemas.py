from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

MonitorMode = Literal["all", "missing", "future", "from_chapter", "latest_volume", "none"]


def _names(value):
    """Stored comma/newline-joined name lists are served as lists."""
    if isinstance(value, str):
        from .automation import split_names

        return split_names(value)
    return value or []


def _lines(value):
    if isinstance(value, str):
        from .automation import split_lines

        return split_lines(value)
    return value or []


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
    excluded: bool
    monitored: bool
    downloaded: bool
    file_path: str
    available_sources: str | None
    file_source: str = ""
    file_group: str = ""


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
    monitor_mode: str = "all"
    monitor_from: float | None = None


class SeriesDetailOut(SeriesOut):
    chapters: list[ChapterOut] = []
    source_links: list[SourceLinkOut] = []
    refreshing: bool = False  # a full refresh is running in the background
    source_priority: list[str] = []
    blocked_sources: list[str] = []
    preferred_groups: list[str] = []
    blocked_groups: list[str] = []
    upgrades_enabled: bool = False
    upgrade_cutoff: str = ""
    merge_volumes: bool = False
    # enabled download sources in global priority order, and in the order
    # grabs try them for this series (override applied, blocked removed)
    global_source_order: list[str] = []
    effective_source_order: list[str] = []
    # release rhythm from MangaUpdates release dates (see release_schedule)
    cadence_days: float | None = None
    cadence_label: str = ""
    last_released_at: datetime | None = None
    next_expected_at: datetime | None = None

    @field_validator("source_priority", "blocked_sources", mode="before")
    @classmethod
    def _split_sources(cls, value):
        return _names(value)

    @field_validator("preferred_groups", "blocked_groups", mode="before")
    @classmethod
    def _split_groups(cls, value):
        return _lines(value)


class AddSeriesIn(BaseModel):
    # exactly one of the provider ids (MangaUpdates is the primary provider)
    mangaupdates_id: int | None = None
    anilist_id: int | None = None
    root_folder_id: int
    monitored: bool = True
    monitor_mode: MonitorMode = "all"
    # first monitored chapter for monitor_mode="from_chapter"
    monitor_from: float | None = None
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
    # setting a mode re-applies it to every chapter
    monitor_mode: MonitorMode | None = None
    monitor_from: float | None = None
    # [] restores the global order / unblocks everything
    source_priority: list[str] | None = None
    blocked_sources: list[str] | None = None
    preferred_groups: list[str] | None = None
    blocked_groups: list[str] | None = None
    upgrades_enabled: bool | None = None
    upgrade_cutoff: str | None = None
    merge_volumes: bool | None = None


class SeriesEditorIn(BaseModel):
    """One change applied to many series at once; None fields are left alone."""
    series_ids: list[int]
    monitored: bool | None = None
    monitor_mode: MonitorMode | None = None
    monitor_from: float | None = None
    root_folder_id: int | None = None
    # with a root folder change: move each series' folder into the new root
    # (otherwise existing files stay where they are and only new ones land there)
    move_files: bool = False


class SeriesEditorProblemOut(BaseModel):
    series_id: int
    title: str
    detail: str


class SeriesEditorOut(BaseModel):
    updated: int
    moved: int = 0
    problems: list[SeriesEditorProblemOut] = []


class SeriesBulkIn(BaseModel):
    series_ids: list[int]


class SeriesBulkRefreshIn(SeriesBulkIn):
    # also queue the missing chapters each series' monitoring wants
    search_missing: bool = False


class SeriesBulkOut(BaseModel):
    count: int


class SeriesGroupOut(BaseModel):
    """A scanlation group a linked source offers for the series."""
    source_name: str
    group: str
    chapters: int


class VolumeMergeOut(BaseModel):
    merged: list[int]


class ChapterMonitorIn(BaseModel):
    chapter_ids: list[int]
    monitored: bool


class ChapterMetadataIn(BaseModel):
    """User-confirmed chapter metadata. Locks survive future refreshes."""
    title: str = ""
    volume: int | None = Field(default=None, ge=1)
    title_locked: bool = True
    volume_locked: bool = True
    excluded: bool | None = None


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
    group: str = ""  # direct: scanlation group, when the source says
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
    group: str = ""
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


class KavitaTestIn(BaseModel):
    url: str
    api_key: str = ""


class KavitaLibraryOut(BaseModel):
    id: int
    name: str
    folders: list[str] = []


class KavitaTestOut(BaseModel):
    ok: bool
    version: str
    libraries: list[KavitaLibraryOut]


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


# ---------------------------------------------------------------- calendar

class CalendarReleaseOut(BaseModel):
    series_id: int
    series_title: str
    cover_url: str
    chapter_id: int
    number: float
    volume: int | None
    title: str
    released_at: datetime
    downloaded: bool
    monitored: bool


class CalendarExpectedOut(BaseModel):
    series_id: int
    series_title: str
    cover_url: str
    expected_at: datetime
    cadence_days: float
    cadence_label: str
    last_released_at: datetime
    last_number: float | None
    overdue: bool  # the expected day has passed without a new release
    monitored: bool


class CalendarOut(BaseModel):
    released: list[CalendarReleaseOut]
    expected: list[CalendarExpectedOut]


# ---------------------------------------------------- related / recommended

class RelatedTitleOut(BaseModel):
    provider: str  # anilist | mangaupdates — which id adding it would use
    provider_id: str
    title: str
    english_title: str = ""
    alt_titles: list[str] = []
    cover_url: str = ""
    year: int | None = None
    status: str = "unknown"
    format: str = ""  # MANGA, ONE_SHOT, NOVEL … when the provider says
    relation: str  # "Sequel", "Side story", "Recommended" …
    in_library_series_id: int | None = None


class RelatedOut(BaseModel):
    relations: list[RelatedTitleOut]
    recommendations: list[RelatedTitleOut]


# ------------------------------------------------------------ bulk import

class ImportFolderOut(BaseModel):
    name: str
    path: str
    file_count: int
    query: str  # the folder name cleaned into a search query


class ImportMatchOut(BaseModel):
    candidates: list[MetadataResult]
    best: int | None  # index of the confident match, None = needs a human


class LibraryImportItemIn(BaseModel):
    folder_name: str
    provider: Literal["mangaupdates", "anilist"]
    provider_id: int
    # the entry's titles, so a library series from another provider is
    # recognised as the same manga
    title: str = ""
    english_title: str = ""
    alt_titles: list[str] = Field(default_factory=list)


class LibraryImportIn(BaseModel):
    root_folder_id: int
    items: list[LibraryImportItemIn]
    monitored: bool = True
    monitor_mode: MonitorMode = "all"
    monitor_from: float | None = None
    search_now: bool = False


class LibraryImportResultOut(BaseModel):
    folder_name: str
    status: Literal["added", "exists", "failed"]
    series_id: int | None = None
    detail: str = ""


# ------------------------------------------------------------ import lists

ImportListKind = Literal["anilist", "myanimelist", "mangadex", "mangaupdates"]


class ImportListIn(BaseModel):
    name: str
    kind: ImportListKind
    enabled: bool = True
    username: str = ""  # AniList / MyAnimeList / MangaUpdates account
    password: str = ""  # MangaUpdates only (its lists need a login)
    client_id: str = ""  # MyAnimeList API client id
    statuses: list[str] = Field(default_factory=list)  # empty = the provider default
    root_folder_id: int
    monitored: bool = True
    monitor_mode: MonitorMode = "all"
    search_now: bool = False


class ImportListOut(ImportListIn):
    id: int
    last_synced_at: datetime | None = None
    last_error: str = ""
    entry_counts: dict[str, int] = {}


class ImportListEntryOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    key: str
    title: str
    anilist_id: int | None
    mangaupdates_id: int | None
    status: str  # added | existing | failed | skipped
    detail: str
    series_id: int | None
    first_seen_at: datetime


class ImportListPreviewItemOut(BaseModel):
    key: str
    title: str
    cover_url: str = ""
    year: int | None = None
    # add = a sync would add it; in_library = already there; seen = handled
    # by an earlier sync (added, skipped, or deleted since)
    action: Literal["add", "in_library", "seen"]
    series_id: int | None = None


class ImportListSyncOut(BaseModel):
    fetched: int
    added: int
    existing: int
    failed: int
