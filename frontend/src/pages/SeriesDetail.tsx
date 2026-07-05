import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useNavigate, useParams } from "react-router-dom";
import { api } from "../api/client";
import type {
  Chapter,
  Release,
  ScanResult,
  SeriesDetail as SeriesDetailType,
} from "../api/types";
import {
  chapterLabel,
  formatBytes,
  Modal,
  Spinner,
  statusPill,
  Toolbar,
} from "../components/common";
import { FilesModal, FoldersPanel, RenameModal, SourcesModal } from "../components/LibraryTools";

function InteractiveSearch({
  seriesId,
  chapterId,
  title,
  onClose,
}: {
  seriesId: number;
  chapterId?: number;
  title: string;
  onClose: () => void;
}) {
  const queryClient = useQueryClient();
  const params = chapterId != null ? `chapter_id=${chapterId}` : `series_id=${seriesId}`;
  const { data, isLoading, isError, error } = useQuery({
    queryKey: ["releases", params],
    queryFn: () => api.get<Release[]>(`/search/releases?${params}`),
  });

  const grab = useMutation({
    mutationFn: (release: Release) =>
      api.post("/queue/grab", release.kind === "direct"
        ? { chapter_id: chapterId, source_name: release.source_name, external_id: release.external_id }
        : { series_id: seriesId, magnet: release.magnet, title: release.title }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["queue"] });
      onClose();
    },
  });

  return (
    <Modal title={`Search — ${title}`} onClose={onClose}>
      {isLoading ? (
        <Spinner />
      ) : isError ? (
        <div className="error-banner">{(error as Error).message}</div>
      ) : !data || data.length === 0 ? (
        <p style={{ color: "var(--text-dim)" }}>
          No releases found. Check source links and that sources are enabled in Settings.
        </p>
      ) : (
        <>
          {grab.isError && <div className="error-banner">{(grab.error as Error).message}</div>}
          <table className="data-table">
            <thead>
              <tr>
                <th>Source</th>
                <th>Title</th>
                <th>Size</th>
                <th>Peers</th>
                <th></th>
              </tr>
            </thead>
            <tbody>
              {data.map((r, i) => (
                <tr key={i}>
                  <td>
                    <span className={`pill ${r.kind === "torrent" ? "orange" : "blue"}`}>
                      {r.source_name}
                    </span>
                  </td>
                  <td>
                    {r.url ? (
                      <a href={r.url} target="_blank" rel="noreferrer" style={{ color: "var(--info)" }}>
                        {r.title}
                      </a>
                    ) : (
                      r.title
                    )}
                  </td>
                  <td>{r.kind === "torrent" ? formatBytes(r.size_bytes) : "—"}</td>
                  <td>{r.kind === "torrent" ? `${r.seeders}/${r.leechers}` : "—"}</td>
                  <td>
                    <button
                      className="btn icon-btn"
                      title="Grab"
                      disabled={grab.isPending}
                      onClick={() => grab.mutate(r)}
                    >
                      ⇓
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </>
      )}
    </Modal>
  );
}

function groupByVolume(chapters: Chapter[]): { volume: number | null; chapters: Chapter[] }[] {
  const byVolume = new Map<number | null, Chapter[]>();
  for (const ch of chapters) {
    const key = ch.volume;
    if (!byVolume.has(key)) byVolume.set(key, []);
    byVolume.get(key)!.push(ch);
  }
  // volume-less chapters first (usually the newest, not yet collected),
  // then volumes descending — like Sonarr's latest-season-on-top
  return [...byVolume.entries()]
    .sort(([a], [b]) => (a === null ? -1 : b === null ? 1 : b - a))
    .map(([volume, chs]) => ({ volume, chapters: chs.sort((a, b) => b.number - a.number) }));
}

export default function SeriesDetail() {
  const { id } = useParams();
  const seriesId = Number(id);
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const [search, setSearch] = useState<{ chapterId?: number; title: string } | null>(null);
  const [collapsed, setCollapsed] = useState<Record<string, boolean>>({});
  const [showRename, setShowRename] = useState(false);
  const [showFiles, setShowFiles] = useState(false);
  const [showSources, setShowSources] = useState(false);
  const [scanResult, setScanResult] = useState<ScanResult | null>(null);

  const { data: series, isLoading } = useQuery({
    queryKey: ["series", seriesId],
    queryFn: () => api.get<SeriesDetailType>(`/series/${seriesId}`),
    refetchInterval: 10000,
  });

  const invalidate = () => {
    queryClient.invalidateQueries({ queryKey: ["series", seriesId] });
    queryClient.invalidateQueries({ queryKey: ["series"] });
  };

  const toggleMonitor = useMutation({
    mutationFn: () => api.put(`/series/${seriesId}`, { monitored: !series?.monitored }),
    onSuccess: invalidate,
  });

  const refresh = useMutation({
    mutationFn: () => api.post(`/series/${seriesId}/refresh`),
    onSuccess: () => setTimeout(invalidate, 4000),
  });

  const scan = useMutation({
    mutationFn: () => api.post<ScanResult>(`/series/${seriesId}/scan`),
    onSuccess: (res) => {
      setScanResult(res);
      invalidate();
    },
  });


  const deleteSeries = useMutation({
    mutationFn: () => api.del(`/series/${seriesId}`),
    onSuccess: () => {
      invalidate();
      navigate("/");
    },
  });

  const toggleChapter = useMutation({
    mutationFn: (args: { chapterIds: number[]; monitored: boolean }) =>
      api.put(`/series/${seriesId}/chapters/monitor`, {
        chapter_ids: args.chapterIds,
        monitored: args.monitored,
      }),
    onSuccess: invalidate,
  });

  if (isLoading || !series) {
    return (
      <>
        <Toolbar title="Series" />
        <Spinner />
      </>
    );
  }

  const hasVolumes = series.chapters.some((c) => c.volume !== null);
  const groups = hasVolumes
    ? groupByVolume(series.chapters)
    : [{ volume: null, chapters: [...series.chapters].sort((a, b) => b.number - a.number) }];

  const chapterRows = (chapters: Chapter[]) => (
    <table className="data-table">
      <thead>
        <tr>
          <th style={{ width: 36 }}></th>
          <th style={{ width: 130 }}>Chapter</th>
          <th>Title</th>
          <th style={{ width: 120 }}>Status</th>
          <th style={{ width: 90 }}></th>
        </tr>
      </thead>
      <tbody>
        {chapters.map((ch) => (
          <tr key={ch.id}>
            <td>
              <button
                className={`monitor-toggle${ch.monitored ? " on" : ""}`}
                title={ch.monitored ? "Monitored" : "Unmonitored"}
                onClick={() =>
                  toggleChapter.mutate({ chapterIds: [ch.id], monitored: !ch.monitored })
                }
              >
                {ch.monitored ? "🔖" : "◻"}
              </button>
            </td>
            <td>{chapterLabel(ch.number, ch.volume)}</td>
            <td style={{ color: ch.title ? "inherit" : "var(--text-faint)" }}>
              {ch.title || "—"}
            </td>
            <td>
              {ch.downloaded ? (
                <span className="pill green" title={ch.file_path}>
                  Downloaded
                </span>
              ) : (
                <span className="pill gray">Missing</span>
              )}
            </td>
            <td>
              <button
                className="btn icon-btn"
                title="Interactive search"
                onClick={() =>
                  setSearch({
                    chapterId: ch.id,
                    title: `${series.title} ${chapterLabel(ch.number, ch.volume)}`,
                  })
                }
              >
                🔍
              </button>
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  );

  return (
    <>
      <Toolbar title={series.title}>
        <button className="btn" onClick={() => refresh.mutate()} disabled={refresh.isPending}>
          ⟳ Refresh
        </button>
        <button className="btn" onClick={() => scan.mutate()} disabled={scan.isPending}>
          {scan.isPending ? "Scanning…" : "🗂 Scan Disk"}
        </button>
        <button className="btn" onClick={() => setShowFiles(true)}>
          📄 Files
        </button>
        <button className="btn" onClick={() => setShowRename(true)}>
          ✏️ Rename
        </button>
        <button
          className="btn"
          onClick={() => setSearch({ title: `${series.title} (all releases)` })}
        >
          🔍 Search Releases
        </button>
        <button className="btn" onClick={() => toggleMonitor.mutate()}>
          {series.monitored ? "🔖 Monitored" : "◻ Unmonitored"}
        </button>
        <button
          className="btn danger"
          onClick={() => {
            if (confirm(`Remove "${series.title}" from library? Files on disk are kept.`))
              deleteSeries.mutate();
          }}
        >
          ✕
        </button>
      </Toolbar>
      <div className="content">
        {scanResult && (
          <div className="scan-banner" onClick={() => setScanResult(null)}>
            <strong>Scan complete.</strong> {scanResult.matched_chapters} chapter
            {scanResult.matched_chapters === 1 ? "" : "s"} matched
            {scanResult.volume_files > 0 && `, ${scanResult.volume_files} volume file(s)`}
            {scanResult.unmatched.length > 0 &&
              `, ${scanResult.unmatched.length} unmatched`}
            {scanResult.cleared > 0 && `, ${scanResult.cleared} cleared (missing)`}
            {!scanResult.folder_exists && " — folder not found on disk"}
            {scanResult.unmatched.length > 0 && (
              <button className="btn sm" onClick={() => setShowFiles(true)}>
                Review files
              </button>
            )}
          </div>
        )}
        <div
          className="series-header"
          style={series.banner_url ? { backgroundImage: `url(${series.banner_url})` } : {}}
        >
          {series.cover_url && <img className="cover" src={series.cover_url} alt="" />}
          <div>
            <h2>
              {series.title}{" "}
              {series.year && <span style={{ color: "var(--text-dim)" }}>({series.year})</span>}
            </h2>
            <div className="series-meta">
              <span className={`pill ${statusPill[series.status] ?? "gray"}`}>{series.status}</span>
              <span>
                {series.downloaded_count} / {series.chapter_count} chapters
              </span>
              {series.total_volumes && <span>{series.total_volumes} volumes</span>}
            </div>
            <div style={{ marginBottom: 10 }}>
              {series.genres
                .split(",")
                .filter(Boolean)
                .map((g) => (
                  <span className="tag" key={g}>
                    {g}
                  </span>
                ))}
            </div>
            <div className="series-desc" dangerouslySetInnerHTML={{ __html: series.description }} />
            <div style={{ marginTop: 12, display: "flex", alignItems: "center", gap: 6, flexWrap: "wrap" }}>
              {series.source_links.map((sl) => (
                <span className="tag" key={sl.id} title={sl.external_title}>
                  🔗 {sl.source_name}
                </span>
              ))}
              <button className="btn sm" onClick={() => setShowSources(true)}>
                {series.source_links.length ? "Edit sources" : "Add sources"}
              </button>
            </div>
            <FoldersPanel seriesId={seriesId} onChanged={invalidate} />
          </div>
        </div>

        {series.chapters.length === 0 ? (
          <p style={{ color: "var(--text-dim)" }}>
            No chapters found yet — sources may still be syncing. Use Refresh to retry.
          </p>
        ) : !hasVolumes ? (
          chapterRows(groups[0].chapters)
        ) : (
          groups.map(({ volume, chapters }) => {
            const key = volume === null ? "none" : String(volume);
            const isCollapsed = collapsed[key] ?? false;
            const downloaded = chapters.filter((c) => c.downloaded).length;
            const allMonitored = chapters.every((c) => c.monitored);
            return (
              <div className="volume-group" key={key}>
                <div
                  className="volume-header"
                  onClick={() => setCollapsed({ ...collapsed, [key]: !isCollapsed })}
                >
                  <span className="chevron">{isCollapsed ? "▸" : "▾"}</span>
                  <button
                    className={`monitor-toggle${allMonitored ? " on" : ""}`}
                    title={allMonitored ? "Unmonitor this volume" : "Monitor this volume"}
                    onClick={(e) => {
                      e.stopPropagation();
                      toggleChapter.mutate({
                        chapterIds: chapters.map((c) => c.id),
                        monitored: !allMonitored,
                      });
                    }}
                  >
                    {allMonitored ? "🔖" : "◻"}
                  </button>
                  <h4>{volume === null ? "Chapters without volume" : `Volume ${volume}`}</h4>
                  <span
                    className={`pill ${downloaded === chapters.length ? "green" : "gray"}`}
                  >
                    {downloaded} / {chapters.length}
                  </span>
                </div>
                {!isCollapsed && chapterRows(chapters)}
              </div>
            );
          })
        )}
      </div>

      {search && (
        <InteractiveSearch
          seriesId={seriesId}
          chapterId={search.chapterId}
          title={search.title}
          onClose={() => setSearch(null)}
        />
      )}
      {showRename && (
        <RenameModal seriesId={seriesId} onClose={() => setShowRename(false)} onDone={invalidate} />
      )}
      {showFiles && (
        <FilesModal
          seriesId={seriesId}
          chapters={series.chapters}
          onClose={() => setShowFiles(false)}
          onChanged={invalidate}
        />
      )}
      {showSources && (
        <SourcesModal
          seriesId={seriesId}
          links={series.source_links}
          onClose={() => setShowSources(false)}
          onChanged={invalidate}
        />
      )}
    </>
  );
}
