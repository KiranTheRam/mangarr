import { useEffect, useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useNavigate, useParams } from "react-router-dom";
import { api } from "../api/client";
import type {
  Chapter,
  QueueItem,
  Release,
  ScanResult,
  SeriesDetail as SeriesDetailType,
  VolumeMappingRow,
  VolumeResyncPreview,
  VolumeResyncResult,
} from "../api/types";
import {
  chapterLabel,
  formatBytes,
  Modal,
  Spinner,
  statusPill,
  Toolbar,
} from "../components/common";
import {
  CleanupModal,
  FilesModal,
  FoldersPanel,
  RenameModal,
  SourcesModal,
} from "../components/LibraryTools";
import { sanitizeDescription } from "../sanitize";

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

  const [activeSource, setActiveSource] = useState("");
  const [selectedDirect, setSelectedDirect] = useState<Set<string>>(() => new Set());
  const sourceNames = useMemo(
    () => [...new Set((data ?? []).map((release) => release.source_name))],
    [data],
  );
  const visibleReleases = useMemo(
    () => (data ?? []).filter((release) => release.source_name === activeSource),
    [data, activeSource],
  );
  const directReleases = visibleReleases.filter((release) => release.kind === "direct");
  const directSelectable = directReleases.filter(
    (release) => (release.chapter_id ?? chapterId) != null,
  );
  const releaseKey = (release: Release) =>
    `${release.source_name}:${release.external_id}:${release.chapter_id ?? chapterId ?? ""}`;
  const selectedVisibleDirect = directSelectable.filter((release) =>
    selectedDirect.has(releaseKey(release)),
  );
  const allVisibleDirectSelected =
    directSelectable.length > 0 && selectedVisibleDirect.length === directSelectable.length;

  useEffect(() => {
    if (!activeSource && sourceNames.length > 0) {
      setActiveSource(sourceNames[0]);
    } else if (activeSource && sourceNames.length > 0 && !sourceNames.includes(activeSource)) {
      setActiveSource(sourceNames[0]);
    }
  }, [activeSource, sourceNames]);

  const grabRelease = (release: Release) => {
    if (release.kind === "direct") {
      const directChapterId = release.chapter_id ?? chapterId;
      if (directChapterId == null) {
        throw new Error("Direct release is not linked to a local chapter");
      }
      return api.post("/queue/grab", {
        chapter_id: directChapterId,
        source_name: release.source_name,
        external_id: release.external_id,
      });
    }
    return api.post("/queue/grab", {
      series_id: seriesId,
      magnet: release.magnet,
      title: release.title,
    });
  };

  const grab = useMutation({
    mutationFn: grabRelease,
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["queue"] });
      onClose();
    },
  });

  const grabSelected = useMutation({
    mutationFn: async (releases: Release[]) => {
      for (const release of releases) {
        await grabRelease(release);
      }
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["queue"] });
      onClose();
    },
  });

  const toggleDirect = (release: Release) => {
    const key = releaseKey(release);
    setSelectedDirect((current) => {
      const next = new Set(current);
      if (next.has(key)) {
        next.delete(key);
      } else {
        next.add(key);
      }
      return next;
    });
  };

  const toggleAllVisibleDirect = () => {
    setSelectedDirect((current) => {
      const next = new Set(current);
      for (const release of directSelectable) {
        const key = releaseKey(release);
        if (allVisibleDirectSelected) {
          next.delete(key);
        } else {
          next.add(key);
        }
      }
      return next;
    });
  };

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
          {grabSelected.isError && (
            <div className="error-banner">{(grabSelected.error as Error).message}</div>
          )}
          <div className="source-tabs">
            {sourceNames.map((source) => {
              const sourceCount = (data ?? []).filter((release) => release.source_name === source).length;
              return (
                <button
                  className={`source-tab${source === activeSource ? " active" : ""}`}
                  key={source}
                  onClick={() => setActiveSource(source)}
                >
                  {source}
                  <span>{sourceCount}</span>
                </button>
              );
            })}
          </div>
          {directSelectable.length > 0 && (
            <div className="release-actions">
              <button className="btn sm" onClick={toggleAllVisibleDirect}>
                {allVisibleDirectSelected ? "Clear selected" : "Select all"}
              </button>
              <span>{selectedVisibleDirect.length} selected</span>
              <button
                className="btn primary sm"
                disabled={selectedVisibleDirect.length === 0 || grabSelected.isPending}
                onClick={() => grabSelected.mutate(selectedVisibleDirect)}
              >
                {grabSelected.isPending ? "Grabbing…" : "Grab selected"}
              </button>
            </div>
          )}
          <table className="data-table card-table release-table">
            <thead>
              <tr>
                {directSelectable.length > 0 && <th style={{ width: 42 }}></th>}
                <th>Title</th>
                <th>Size</th>
                <th>Peers</th>
                <th></th>
              </tr>
            </thead>
            <tbody>
              {visibleReleases.map((r, i) => (
                <tr key={`${r.source_name}-${r.external_id || r.magnet || i}`}>
                  {directSelectable.length > 0 && (
                    <td className="cell-select">
                      {r.kind === "direct" && (r.chapter_id ?? chapterId) != null && (
                        <input
                          type="checkbox"
                          checked={selectedDirect.has(releaseKey(r))}
                          onChange={() => toggleDirect(r)}
                        />
                      )}
                    </td>
                  )}
                  <td className="cell-rtitle">
                    {r.url ? (
                      <a href={r.url} target="_blank" rel="noreferrer" style={{ color: "var(--info)" }}>
                        {r.title}
                      </a>
                    ) : (
                      r.title
                    )}
                  </td>
                  <td className="cell-size">{r.kind === "torrent" ? formatBytes(r.size_bytes) : "—"}</td>
                  <td className="cell-peers">{r.kind === "torrent" ? `${r.seeders}/${r.leechers}` : "—"}</td>
                  <td className="cell-grab">
                    <button
                      className="btn icon-btn"
                      title="Grab"
                      disabled={
                        grab.isPending ||
                        grabSelected.isPending ||
                        (r.kind === "direct" && (r.chapter_id ?? chapterId) == null)
                      }
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

function chapterNumberLabel(n: number): string {
  return Number.isInteger(n) ? n.toString() : n.toFixed(1);
}

// contiguous chapter runs sharing a volume, in chapter order — the compact
// form of a full chapter→volume mapping ("Vol. 2 · Ch. 11–18")
function mappingRuns(mapping: VolumeMappingRow[]) {
  const runs: { volume: number | null; start: number; end: number; count: number }[] = [];
  for (const row of [...mapping].sort((a, b) => a.number - b.number)) {
    const last = runs[runs.length - 1];
    if (last && last.volume === row.volume) {
      last.end = row.number;
      last.count += 1;
    } else {
      runs.push({ volume: row.volume, start: row.number, end: row.number, count: 1 });
    }
  }
  return runs;
}

function VolumeResyncModal({
  preview,
  source,
  onPick,
  onApply,
  applying,
  onClose,
}: {
  preview: VolumeResyncPreview;
  source: string;
  onPick: (source: string) => void;
  onApply: () => void;
  applying: boolean;
  onClose: () => void;
}) {
  const selected =
    preview.candidates.find((c) => c.source === source) ?? preview.candidates[0];
  const sourceLabel = (value: string) => value === "auto" ? "Recommended merge" : value;
  const vol = (v: number | null) => (v == null ? "—" : `Vol. ${v}`);
  const bestHasChanges = preview.candidates[0]?.has_changes;
  // when nothing would change, the interesting view is the mapping itself
  const [view, setView] = useState<"changes" | "mapping">(
    bestHasChanges ? "changes" : "mapping",
  );
  return (
    <Modal title="Volume mappings" onClose={onClose}>
      <p style={{ color: "var(--text-dim)", marginBottom: 12 }}>
        {bestHasChanges
          ? "Refresh finished, and source volume data would change this series' " +
            "chapter–volume assignments or file coverage. Pick which source's " +
            "mapping to apply, or cancel to keep everything as it is."
          : "Refresh finished. Current volume assignments already match the best " +
            "source, but you can still apply a different source's mapping below, " +
            "or close to keep everything as it is."}
      </p>

      {preview.candidates.map((c, i) => (
        <label
          key={c.source}
          style={{ display: "flex", gap: 8, alignItems: "baseline", padding: "4px 0", cursor: "pointer" }}
        >
          <input
            type="radio"
            name="resync-source"
            checked={selected?.source === c.source}
            onChange={() => onPick(c.source)}
          />
          <span>
            <strong>{sourceLabel(c.source)}</strong>
            {i === 0 && (
              <span className="pill green" style={{ marginLeft: 8 }}>
                best match
              </span>
            )}
            <span style={{ color: "var(--text-dim)" }}>
              {" "}
              — {c.map_size} chapters mapped
              {c.has_changes
                ? ` · ${c.changed} change${c.changed === 1 ? "" : "s"}` +
                  (c.repointed > 0 ? ` · ${c.repointed} re-pointed` : "") +
                  (c.cleared > 0 ? ` · ${c.cleared} cleared` : "")
                : " · no changes"}
            </span>
          </span>
        </label>
      ))}

      {selected && (
        <>
          <div style={{ margin: "14px 0 8px", color: "var(--text-dim)", fontSize: 13 }}>
            Applying <strong>{sourceLabel(selected.source)}</strong> leaves {selected.assigned} chapter
            {selected.assigned === 1 ? "" : "s"} assigned to volumes, changes{" "}
            {selected.changed} assignment{selected.changed === 1 ? "" : "s"}
            {selected.repointed > 0 &&
              `, re-points ${selected.repointed} chapter${selected.repointed === 1 ? "" : "s"} to a different file`}
            {selected.cleared > 0 &&
              `, leaves ${selected.cleared} chapter${selected.cleared === 1 ? "" : "s"} no longer backed by a file`}
            .
          </div>
          <div style={{ display: "flex", gap: 8, margin: "10px 0 8px" }}>
            <button
              className={`btn sm${view === "changes" ? " primary" : ""}`}
              onClick={() => setView("changes")}
            >
              Changes ({selected.diff.length})
            </button>
            <button
              className={`btn sm${view === "mapping" ? " primary" : ""}`}
              onClick={() => setView("mapping")}
            >
              Full mapping ({selected.mapping.length} chapters)
            </button>
          </div>
          {view === "changes" ? (
            selected.diff.length > 0 ? (
              <div
                style={{
                  maxHeight: 280,
                  overflowY: "auto",
                  border: "1px solid var(--border)",
                  borderRadius: 6,
                }}
              >
                <table className="data-table">
                  <thead>
                    <tr>
                      <th style={{ textAlign: "left" }}>Chapter</th>
                      <th style={{ textAlign: "left" }}>Current</th>
                      <th style={{ textAlign: "left" }}>New</th>
                    </tr>
                  </thead>
                  <tbody>
                    {selected.diff.map((row) => (
                      <tr key={row.number}>
                        <td>{chapterLabel(row.number)}</td>
                        <td>{vol(row.old_volume)}</td>
                        <td>{vol(row.new_volume)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            ) : (
              <p style={{ color: "var(--text-dim)" }}>
                No changes — {sourceLabel(selected.source)} matches the current volume
                assignments.
              </p>
            )
          ) : (
            <div
              style={{
                maxHeight: 280,
                overflowY: "auto",
                border: "1px solid var(--border)",
                borderRadius: 6,
              }}
            >
              <table className="data-table">
                <thead>
                  <tr>
                    <th style={{ textAlign: "left", width: 120 }}>Volume</th>
                    <th style={{ textAlign: "left" }}>Chapters</th>
                    <th style={{ textAlign: "left", width: 120 }}></th>
                  </tr>
                </thead>
                <tbody>
                  {mappingRuns(selected.mapping).map((run, i) => (
                    <tr key={i}>
                      <td>
                        {run.volume == null ? (
                          <span style={{ color: "var(--text-faint)" }}>Unassigned</span>
                        ) : (
                          `Vol. ${run.volume}`
                        )}
                      </td>
                      <td>
                        {run.start === run.end
                          ? `Ch. ${chapterNumberLabel(run.start)}`
                          : `Ch. ${chapterNumberLabel(run.start)}–${chapterNumberLabel(run.end)}`}
                      </td>
                      <td style={{ color: "var(--text-dim)" }}>
                        {run.count} chapter{run.count === 1 ? "" : "s"}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </>
      )}

      <div style={{ display: "flex", justifyContent: "flex-end", gap: 8, marginTop: 16 }}>
        <button className="btn" onClick={onClose}>
          Keep current volumes
        </button>
        <button className="btn primary" disabled={applying || !selected} onClick={onApply}>
          {applying ? "Applying…" : `Apply ${selected ? sourceLabel(selected.source) : ""} volumes`}
        </button>
      </div>
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

function ChapterMetadataModal({
  seriesId,
  chapter,
  onClose,
  onSaved,
}: {
  seriesId: number;
  chapter: Chapter;
  onClose: () => void;
  onSaved: () => void;
}) {
  const [title, setTitle] = useState(chapter.title);
  const [volume, setVolume] = useState(chapter.volume == null ? "" : String(chapter.volume));
  const [titleLocked, setTitleLocked] = useState(chapter.title_locked);
  const [volumeLocked, setVolumeLocked] = useState(chapter.volume_locked);
  const save = useMutation({
    mutationFn: () => api.put(`/series/${seriesId}/chapters/${chapter.id}/metadata`, {
      title,
      volume: volume.trim() === "" ? null : Number(volume),
      title_locked: titleLocked,
      volume_locked: volumeLocked,
    }),
    onSuccess: () => {
      onSaved();
      onClose();
    },
  });
  return (
    <Modal title={`Edit ${chapterLabel(chapter.number, chapter.volume)}`} onClose={onClose}>
      <div className="form-row">
        <label>Title</label>
        <input
          value={title}
          onChange={(event) => {
            setTitle(event.target.value);
            setTitleLocked(true);
          }}
        />
      </div>
      <div className="form-row">
        <label>Volume</label>
        <input
          type="number"
          min="1"
          value={volume}
          onChange={(event) => {
            setVolume(event.target.value);
            setVolumeLocked(true);
          }}
        />
      </div>
      <label style={{ display: "block", marginBottom: 8 }}>
        <input type="checkbox" checked={titleLocked} onChange={(event) => setTitleLocked(event.target.checked)} />{" "}
        Keep this title during future refreshes
      </label>
      <label style={{ display: "block" }}>
        <input type="checkbox" checked={volumeLocked} onChange={(event) => setVolumeLocked(event.target.checked)} />{" "}
        Keep this volume during future refreshes/resyncs
      </label>
      {save.isError && <div className="error-banner">{(save.error as Error).message}</div>}
      <div style={{ display: "flex", justifyContent: "flex-end", gap: 8, marginTop: 16 }}>
        <button className="btn" onClick={onClose}>Cancel</button>
        <button className="btn primary" disabled={save.isPending || (volume !== "" && Number(volume) < 1)} onClick={() => save.mutate()}>
          {save.isPending ? "Saving…" : "Save"}
        </button>
      </div>
    </Modal>
  );
}

export default function SeriesDetail() {
  const { id } = useParams();
  const seriesId = Number(id);
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const [search, setSearch] = useState<{ chapterId?: number; title: string } | null>(null);
  const [editingChapter, setEditingChapter] = useState<Chapter | null>(null);
  const [collapsed, setCollapsed] = useState<Record<string, boolean>>({});
  // mobile: the synopsis is clamped to a few lines; tapping it toggles the full text
  const [descExpanded, setDescExpanded] = useState(false);
  const [revealed, setRevealed] = useState<Record<string, boolean>>({});
  const toggleReveal = (k: string) => setRevealed((r) => ({ ...r, [k]: !r[k] }));
  const [showRename, setShowRename] = useState(false);
  const [showFiles, setShowFiles] = useState(false);
  const [showSources, setShowSources] = useState(false);
  const [showCleanup, setShowCleanup] = useState(false);
  const [scanResult, setScanResult] = useState<ScanResult | null>(null);
  const [volumeResult, setVolumeResult] = useState<VolumeResyncResult | null>(null);
  const [resyncPreview, setResyncPreview] = useState<VolumeResyncPreview | null>(null);
  const [resyncSource, setResyncSource] = useState("");
  const [workNotice, setWorkNotice] = useState<string | null>(null);

  const showWorkNotice = (message: string, timeout = 9000) => {
    setWorkNotice(message);
    window.setTimeout(() => setWorkNotice((current) => (current === message ? null : current)), timeout);
  };

  const { data: series, isLoading, isError, error } = useQuery({
    queryKey: ["series", seriesId],
    queryFn: () => api.get<SeriesDetailType>(`/series/${seriesId}`),
    enabled: Number.isFinite(seriesId),
    // poll fast while a background refresh is populating the page (e.g.
    // right after adding the series), lazily otherwise
    refetchInterval: (query) => (query.state.data?.refreshing ? 2000 : 10000),
  });

  const { data: queue } = useQuery({
    queryKey: ["queue"],
    queryFn: () => api.get<QueueItem[]>("/queue"),
    refetchInterval: 2000,
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
    mutationFn: async () => {
      // refresh synchronously, then dry-run the volume resync to learn
      // whether applying it would change anything worth confirming
      await api.post(`/series/${seriesId}/refresh?wait=true`);
      return api.get<VolumeResyncPreview>(`/series/${seriesId}/volumes/resync-preview`);
    },
    onSuccess: (preview) => {
      invalidate();
      // always offer the source options when any exist, so a resync can be
      // triggered even when the current mapping already matches the best one
      if (preview.candidates.length > 0) {
        setResyncSource(preview.candidates[0].source);
        setResyncPreview(preview);
      } else {
        showWorkNotice("Refresh complete — no linked source provides volume data.");
      }
    },
  });

  const scan = useMutation({
    mutationFn: () => api.post<ScanResult>(`/series/${seriesId}/scan`),
    onSuccess: (res) => {
      setScanResult(res);
      invalidate();
    },
  });


  const applyResync = useMutation({
    mutationFn: (source: string) =>
      api.post<VolumeResyncResult>(`/series/${seriesId}/volumes/resync`, { source }),
    onSuccess: (res) => {
      setVolumeResult(res);
      setResyncPreview(null);
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

  if (!Number.isFinite(seriesId)) {
    return (
      <>
        <Toolbar title="Series" />
        <div className="content">
          <div className="error-banner">Invalid series id.</div>
        </div>
      </>
    );
  }

  if (isError) {
    return (
      <>
        <Toolbar title="Series" />
        <div className="content">
          <div className="error-banner">{(error as Error).message}</div>
        </div>
      </>
    );
  }

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

  const groupKey = (volume: number | null) => (volume === null ? "none" : String(volume));
  const allCollapsed =
    hasVolumes && groups.every(({ volume }) => collapsed[groupKey(volume)] ?? false);
  const setAllCollapsed = (value: boolean) =>
    setCollapsed(Object.fromEntries(groups.map(({ volume }) => [groupKey(volume), value])));

  // how many chapters share each file — a file used by >1 chapter is a
  // whole-volume archive (those chapters have no individual file of their own)
  const fileCounts: Record<string, number> = {};
  for (const c of series.chapters) {
    if (c.file_path) fileCounts[c.file_path] = (fileCounts[c.file_path] ?? 0) + 1;
  }
  const isVolumeArchive = (path: string) => (fileCounts[path] ?? 0) > 1;
  const activeDownloads = (queue ?? []).filter((item) => item.series_id === seriesId);
  const busyNotice =
    scan.isPending ? "Scanning disk and matching files…" :
    applyResync.isPending ? "Applying volume resync…" :
    refresh.isPending ? "Refreshing metadata, source links, and chapters…" :
    deleteSeries.isPending ? "Removing series…" :
    toggleMonitor.isPending ? "Updating monitoring…" :
    series?.refreshing
      ? "Setting up — fetching metadata, linking sources, and syncing chapters…" :
    workNotice;
  const hasTopBanners =
    Boolean(busyNotice || scanResult || volumeResult) || activeDownloads.length > 0;

  const chapterRows = (chapters: Chapter[]) => (
    <table className="data-table card-table chapter-table">
      <thead>
        <tr>
          <th style={{ width: 36 }}></th>
          <th style={{ width: 130 }}>Chapter</th>
          <th>Title</th>
          <th style={{ width: 120 }}>Status</th>
          <th style={{ width: 125 }}></th>
        </tr>
      </thead>
      <tbody>
        {chapters.map((ch) => (
          <tr key={ch.id}>
            <td className="cell-monitor">
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
            <td className="cell-chapter">
              {ch.downloaded && ch.file_path && !isVolumeArchive(ch.file_path) ? (
                <button
                  className="link-text"
                  title="Show filename on disk"
                  onClick={() => toggleReveal(`c${ch.id}`)}
                >
                  {chapterLabel(ch.number, ch.volume)}
                </button>
              ) : (
                chapterLabel(ch.number, ch.volume)
              )}
              {revealed[`c${ch.id}`] && ch.file_path && (
                <div className="filepath">{ch.file_path}</div>
              )}
            </td>
            <td
              className="cell-title"
              style={{ color: ch.title ? "inherit" : "var(--text-faint)" }}
              title={ch.title_source ? `Title source: ${ch.title_source}${ch.title_locked ? " (locked)" : ""}` : undefined}
            >
              {ch.title || "—"}
            </td>
            <td className="cell-status">
              {ch.downloaded ? (
                <span className="pill green" title={ch.file_path}>
                  Downloaded
                </span>
              ) : (
                <span className="pill gray">Missing</span>
              )}
            </td>
            <td className="cell-actions">
              <button
                className="btn icon-btn"
                title={`Edit title and volume${ch.volume_source ? ` (volume source: ${ch.volume_source})` : ""}`}
                onClick={() => setEditingChapter(ch)}
              >
                ✏️
              </button>
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
      <Toolbar className="series-toolbar">
        <button
          className="btn"
          title="Refresh metadata, source links, chapters, and library state; volume changes ask for confirmation"
          onClick={() => refresh.mutate()}
          disabled={refresh.isPending}
        >
          {refresh.isPending ? "Refreshing…" : "⟳ Refresh"}
        </button>
        <button
          className="btn"
          title="Scan this series' folders and match files on disk"
          onClick={() => scan.mutate()}
          disabled={scan.isPending}
        >
          {scan.isPending ? "Scanning…" : "🗂 Scan Disk"}
        </button>
        <button
          className="btn"
          title="Browse detected files and manually map unmatched files"
          onClick={() => setShowFiles(true)}
        >
          📄 Files
        </button>
        <button
          className="btn"
          title="Preview and apply the configured file naming pattern"
          onClick={() => setShowRename(true)}
        >
          ✏️ Rename
        </button>
        <button
          className="btn"
          title="Find duplicate or orphaned files that can be cleaned up"
          onClick={() => setShowCleanup(true)}
        >
          🧹 Clean up
        </button>
        <button
          className="btn"
          title="Search direct sources and torrent indexers for this series"
          onClick={() => setSearch({ title: `${series.title} (all releases)` })}
        >
          🔍 Search Releases
        </button>
        <button
          className="btn"
          title={series.monitored ? "Stop automatically grabbing new chapters" : "Automatically grab new chapters"}
          onClick={() => toggleMonitor.mutate()}
          disabled={toggleMonitor.isPending}
        >
          {series.monitored ? "🔖 Monitored" : "◻ Unmonitored"}
        </button>
        <button
          className="btn danger"
          title="Remove this series from Mangarr without deleting files"
          disabled={deleteSeries.isPending}
          onClick={() => {
            if (confirm(`Remove "${series.title}" from library? Files on disk are kept.`))
              deleteSeries.mutate();
          }}
        >
          ✕
        </button>
      </Toolbar>
      <div className="content">
        {busyNotice && (
          <div className="activity-banner">
            <span className="mini-spinner" />
            <strong>Working.</strong>
            <span>{busyNotice}</span>
          </div>
        )}
        {activeDownloads.length > 0 && (
          <div className="activity-banner">
            <span className="mini-spinner" />
            <strong>Pulling content.</strong>
            {activeDownloads.slice(0, 3).map((item) => (
              <span className="activity-chip" key={item.id} title={item.title || item.series_title}>
                {item.kind} · {item.status} · {Math.round(item.progress * 100)}%
              </span>
            ))}
            {activeDownloads.length > 3 && (
              <span className="activity-chip">+{activeDownloads.length - 3} more</span>
            )}
          </div>
        )}
        {volumeResult && (
          <div className="scan-banner" onClick={() => setVolumeResult(null)}>
            {volumeResult.has_data ? (
              <>
                <strong>Volumes resynced.</strong> {volumeResult.assigned} chapter
                {volumeResult.assigned === 1 ? "" : "s"} assigned to volumes
                {volumeResult.changed > 0 && `, ${volumeResult.changed} changed`}
                {volumeResult.repointed > 0 &&
                  `, ${volumeResult.repointed} re-pointed to a different file`}
                {volumeResult.cleared > 0 &&
                  `, ${volumeResult.cleared} no longer backed by a file (now missing)`}
              </>
            ) : (
              <>
                <strong>No volume data.</strong> None of this series' linked sources
                provide volume→chapter assignments; existing values were left as-is.
              </>
            )}
          </div>
        )}
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
          className={`series-header${hasTopBanners ? "" : " flush-top"}`}
          style={series.banner_url ? { backgroundImage: `url(${series.banner_url})` } : {}}
        >
          {series.cover_url && <img className="cover" src={series.cover_url} alt="" />}
          <div className="series-info">
            <div className="series-title-block">
              <h2>
                {series.title}{" "}
                {series.year && <span style={{ color: "var(--text-dim)" }}>({series.year})</span>}
              </h2>
              {series.english_title && series.english_title !== series.title && (
                <div className="alt-title-line series-alt-title">
                  English: {series.english_title}
                </div>
              )}
              <div className="series-meta">
                <span className={`pill ${statusPill[series.status] ?? "gray"}`}>{series.status}</span>
                <span>
                  {series.downloaded_count} / {series.chapter_count} chapters
                </span>
                {series.special_count > 0 && (
                  <span
                    title="Decimal chapters (60.5 …) are specials — searched for like any other chapter, but they don't count toward completion"
                  >
                    {series.special_downloaded_count} / {series.special_count} specials
                  </span>
                )}
                {series.total_volumes && <span>{series.total_volumes} volumes</span>}
              </div>
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
            <div
              className={`series-desc${descExpanded ? " expanded" : ""}`}
              onClick={() => setDescExpanded((v) => !v)}
              dangerouslySetInnerHTML={{ __html: sanitizeDescription(series.description) }}
            />
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
          <>
          <div style={{ display: "flex", justifyContent: "flex-end", marginBottom: 6 }}>
            <button
              className="btn sm"
              title={allCollapsed ? "Expand every volume section" : "Collapse every volume section"}
              onClick={() => setAllCollapsed(!allCollapsed)}
            >
              {allCollapsed ? "▸ Expand all" : "▾ Collapse all"}
            </button>
          </div>
          {groups.map(({ volume, chapters }) => {
            const key = groupKey(volume);
            const isCollapsed = collapsed[key] ?? false;
            const downloaded = chapters.filter((c) => c.downloaded).length;
            const allMonitored = chapters.every((c) => c.monitored);
            // the single archive file backing this volume, if it is one
            const files = new Set(
              chapters.filter((c) => c.downloaded && c.file_path).map((c) => c.file_path),
            );
            const archiveFile =
              files.size === 1 && isVolumeArchive([...files][0]) ? [...files][0] : null;
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
                  {archiveFile ? (
                    <h4
                      className="link-text"
                      title="Show volume filename on disk"
                      onClick={(e) => {
                        e.stopPropagation();
                        toggleReveal(`v${key}`);
                      }}
                    >
                      Volume {volume}
                    </h4>
                  ) : (
                    <h4>{volume === null ? "Chapters without volume" : `Volume ${volume}`}</h4>
                  )}
                  <span
                    className={`pill ${downloaded === chapters.length ? "green" : "gray"}`}
                  >
                    {downloaded} / {chapters.length}
                  </span>
                </div>
                {revealed[`v${key}`] && archiveFile && (
                  <div className="filepath volume-filepath">{archiveFile}</div>
                )}
                {!isCollapsed && chapterRows(chapters)}
              </div>
            );
          })}
          </>
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
      {editingChapter && (
        <ChapterMetadataModal
          seriesId={seriesId}
          chapter={editingChapter}
          onClose={() => setEditingChapter(null)}
          onSaved={invalidate}
        />
      )}
      {resyncPreview && (
        <VolumeResyncModal
          preview={resyncPreview}
          source={resyncSource}
          onPick={setResyncSource}
          onApply={() => applyResync.mutate(resyncSource)}
          applying={applyResync.isPending}
          onClose={() => setResyncPreview(null)}
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
      {showCleanup && (
        <CleanupModal
          seriesId={seriesId}
          onClose={() => setShowCleanup(false)}
          onDone={invalidate}
        />
      )}
    </>
  );
}
