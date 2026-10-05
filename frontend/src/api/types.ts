export interface RootFolder {
  id: number;
  path: string;
}

export interface KavitaLibrary {
  id: number;
  name: string;
  folders: string[];
}

export interface KavitaTestResult {
  ok: boolean;
  version: string;
  libraries: KavitaLibrary[];
}

export interface SourceLink {
  id: number;
  source_name: string;
  external_id: string;
  external_title: string;
  external_url: string;
}

export interface Chapter {
  id: number;
  number: number;
  volume: number | null;
  title: string;
  title_source: string;
  volume_source: string;
  title_locked: boolean;
  volume_locked: boolean;
  excluded: boolean;
  monitored: boolean;
  downloaded: boolean;
  file_path: string;
  available_sources: string | null;
  // where mangarr got the file ("" = adopted from disk) and its scanlation group
  file_source: string;
  file_group: string;
}

export interface Series {
  id: number;
  anilist_id: number | null;
  mangaupdates_id: number | null;
  title: string;
  english_title: string;
  alt_titles: string;
  description: string;
  status: string;
  year: number | null;
  cover_url: string;
  banner_url: string;
  genres: string;
  monitored: boolean;
  root_folder_id: number | null;
  folder_name: string;
  folder_pinned: boolean;
  total_chapters: number | null;
  total_volumes: number | null;
  added_at: string;
  // main (whole-numbered) chapters only — these define "fully downloaded"
  chapter_count: number;
  downloaded_count: number;
  // decimal chapters (60.5 …), tracked and searched but never blocking completion
  special_count: number;
  special_downloaded_count: number;
  monitor_mode: MonitorMode;
  monitor_from: number | null;
}

export type MonitorMode = "all" | "missing" | "future" | "from_chapter" | "latest_volume" | "none";

/** Result of a library mass edit; series listed in `problems` kept their
 * old root folder (the rest of the edit still applied to them). */
export interface SeriesEditorResult {
  updated: number;
  moved: number;
  problems: { series_id: number; title: string; detail: string }[];
}

export interface SeriesDetail extends Series {
  chapters: Chapter[];
  source_links: SourceLink[];
  refreshing: boolean;
  source_priority: string[];
  blocked_sources: string[];
  preferred_groups: string[];
  blocked_groups: string[];
  upgrades_enabled: boolean;
  upgrade_cutoff: string;
  merge_volumes: boolean;
  // enabled download sources: global order, and the order grabs use here
  global_source_order: string[];
  effective_source_order: string[];
  // release rhythm from MangaUpdates release dates
  cadence_days: number | null;
  cadence_label: string;
  last_released_at: string | null;
  next_expected_at: string | null;
}

export interface SeriesGroup {
  source_name: string;
  group: string;
  chapters: number;
}

export interface MetadataResult {
  provider: string;
  provider_id: string;
  title: string;
  english_title: string;
  alt_titles: string[];
  description: string;
  status: string;
  year: number | null;
  cover_url: string;
  genres: string[];
  total_chapters: number | null;
  total_volumes: number | null;
  in_library: boolean;
}

export interface FolderPreview {
  folder_name: string;
  path: string;
  exists: boolean;
  matched: boolean;
  default_folder_name: string;
}

export interface Release {
  kind: "direct" | "torrent";
  source_name: string;
  title: string;
  chapter_id: number | null;
  chapter_number: number | null;
  external_id: string;
  group: string;
  url: string;
  magnet: string;
  size_bytes: number;
  seeders: number;
  leechers: number;
}

export interface QueueItem {
  id: number;
  series_id: number | null;
  chapter_id: number | null;
  kind: string;
  status: string;
  title: string;
  source_name: string;
  progress: number;
  error: string;
  created_at: string;
  series_title: string;
}

export interface HistoryItem {
  id: number;
  series_id: number | null;
  event: string;
  detail: string;
  source_name: string;
  created_at: string;
  series_title: string;
}

export interface WantedItem {
  chapter_id: number;
  series_id: number;
  series_title: string;
  cover_url: string;
  number: number;
  volume: number | null;
  title: string;
}

export interface SystemStatus {
  version: string;
  series_count: number;
  chapter_count: number;
  downloaded_count: number;
  queue_count: number;
}

export type Settings = Record<string, string>;

export interface ApiKey {
  id: number;
  name: string;
  key: string;
  created_at: string;
  last_used_at: string | null;
}

export interface ScanResult {
  folder: string;
  folder_exists: boolean;
  matched_chapters: number;
  volume_files: number;
  cleared: number;
  unmatched: string[];
}

export interface VolumeResyncResult {
  has_data: boolean;
  assigned: number;
  changed: number;
  repointed: number;
  cleared: number;
}

export interface VolumeDiffRow {
  number: number;
  old_volume: number | null;
  new_volume: number | null;
}

export interface VolumeMappingRow {
  number: number;
  volume: number | null;
}

export interface VolumeCandidate {
  source: string;
  map_size: number;
  assigned: number;
  changed: number;
  repointed: number;
  cleared: number;
  has_changes: boolean;
  diff: VolumeDiffRow[];
  mapping: VolumeMappingRow[];
}

export interface VolumeResyncPreview {
  candidates: VolumeCandidate[];
}

export interface RenameItem {
  chapter_ids: number[];
  current_path: string;
  current_name: string;
  new_path: string;
  new_name: string;
  conflict: boolean;
}

export interface RenameOutcome {
  current_name: string;
  new_name: string;
  status: string;
  detail: string;
}

export interface SeriesFile {
  covered_count: number;
  path: string;
  name: string;
  is_dir: boolean;
  chapter_number: number | null;
  volume_number: number | null;
  matched_chapter_id: number | null;
}

export interface CleanupFile {
  path: string;
  name: string;
  size: number;
  referenced: boolean;
  keep: boolean;
}

export interface CleanupGroup {
  label: string;
  files: CleanupFile[];
}

export interface CleanupPlan {
  groups: CleanupGroup[];
  orphans: CleanupFile[];
  overlaps: CleanupFile[];
}

export interface SourceCandidate {
  source_name: string;
  external_id: string;
  title: string;
  url: string;
  alt_titles: string[];
}

export interface SeriesFolder {
  id: number | null;
  path: string;
  resolved: string;
  primary: boolean;
  exists: boolean;
}

export interface FilesystemEntry {
  name: string;
  path: string;
}

export interface FilesystemList {
  path: string;
  parent: string | null;
  entries: FilesystemEntry[];
}

export interface CalendarRelease {
  series_id: number;
  series_title: string;
  cover_url: string;
  chapter_id: number;
  number: number;
  volume: number | null;
  title: string;
  released_at: string;
  downloaded: boolean;
  monitored: boolean;
}

export interface CalendarExpected {
  series_id: number;
  series_title: string;
  cover_url: string;
  expected_at: string;
  cadence_days: number;
  cadence_label: string;
  last_released_at: string;
  last_number: number | null;
  overdue: boolean;
  monitored: boolean;
}

export interface CalendarData {
  released: CalendarRelease[];
  expected: CalendarExpected[];
}

export interface RelatedTitle {
  provider: "anilist" | "mangaupdates";
  provider_id: string;
  title: string;
  english_title: string;
  alt_titles: string[];
  cover_url: string;
  year: number | null;
  status: string;
  format: string;
  relation: string;
  in_library_series_id: number | null;
}

export interface RelatedData {
  relations: RelatedTitle[];
  recommendations: RelatedTitle[];
}

export interface ImportFolder {
  name: string;
  path: string;
  file_count: number;
  query: string;
}

export interface ImportMatch {
  candidates: MetadataResult[];
  best: number | null;
}

export interface LibraryImportResult {
  folder_name: string;
  status: "added" | "exists" | "failed";
  series_id: number | null;
  detail: string;
}

export type ImportListKind = "anilist" | "myanimelist" | "mangadex" | "mangaupdates";

export interface ImportListProvider {
  kind: ImportListKind;
  label: string;
  statuses: Record<string, string>;
  default_statuses: string[];
  needs_username: boolean;
  needs_password: boolean;
  needs_client_id: boolean;
}

export interface ImportListConfig {
  name: string;
  kind: ImportListKind;
  enabled: boolean;
  username: string;
  password: string;
  client_id: string;
  statuses: string[];
  root_folder_id: number;
  monitored: boolean;
  monitor_mode: MonitorMode;
  search_now: boolean;
}

export interface ImportList extends ImportListConfig {
  id: number;
  last_synced_at: string | null;
  last_error: string;
  entry_counts: Record<string, number>;
}

export interface ImportListEntry {
  id: number;
  key: string;
  title: string;
  anilist_id: number | null;
  mangaupdates_id: number | null;
  status: "added" | "existing" | "failed" | "skipped";
  detail: string;
  series_id: number | null;
  first_seen_at: string;
}

export interface ImportListPreviewItem {
  key: string;
  title: string;
  cover_url: string;
  year: number | null;
  action: "add" | "in_library" | "seen";
  series_id: number | null;
}

export interface ImportListSyncResult {
  fetched: number;
  added: number;
  existing: number;
  failed: number;
}
