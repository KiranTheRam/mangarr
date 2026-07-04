export interface RootFolder {
  id: number;
  path: string;
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
  monitored: boolean;
  downloaded: boolean;
  file_path: string;
}

export interface Series {
  id: number;
  anilist_id: number | null;
  title: string;
  description: string;
  status: string;
  year: number | null;
  cover_url: string;
  banner_url: string;
  genres: string;
  monitored: boolean;
  root_folder_id: number | null;
  folder_name: string;
  total_chapters: number | null;
  total_volumes: number | null;
  added_at: string;
  chapter_count: number;
  downloaded_count: number;
}

export interface SeriesDetail extends Series {
  chapters: Chapter[];
  source_links: SourceLink[];
}

export interface MetadataResult {
  provider: string;
  provider_id: string;
  title: string;
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

export interface Release {
  kind: "direct" | "torrent";
  source_name: string;
  title: string;
  chapter_number: number | null;
  external_id: string;
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
