import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../api/client";
import type {
  Chapter,
  CleanupPlan,
  RenameItem,
  RenameOutcome,
  SeriesFile,
  SeriesFolder,
  SourceCandidate,
  SourceLink,
} from "../api/types";
import { Modal, Spinner, chapterLabel, formatBytes } from "./common";
import { FolderBrowser } from "./FolderBrowser";

/** Lists the folders a series spans (primary + extras) with add/remove and a
 *  way to change the primary. Supports libraries where a series is split
 *  across, e.g., a volumes folder and a chapters folder. */
export function FoldersPanel({
  seriesId,
  onChanged,
}: {
  seriesId: number;
  onChanged: () => void;
}) {
  const queryClient = useQueryClient();
  const [picking, setPicking] = useState<null | "add" | "primary">(null);
  const invalidate = () => {
    queryClient.invalidateQueries({ queryKey: ["folders", seriesId] });
    onChanged();
  };
  const { data } = useQuery({
    queryKey: ["folders", seriesId],
    queryFn: () => api.get<SeriesFolder[]>(`/series/${seriesId}/folders`),
  });

  const addExtra = useMutation({
    mutationFn: (path: string) => api.post(`/series/${seriesId}/folders`, { path }),
    onSuccess: invalidate,
  });
  const removeExtra = useMutation({
    mutationFn: (id: number) => api.del(`/series/${seriesId}/folders/${id}`),
    onSuccess: invalidate,
  });
  const setPrimary = useMutation({
    mutationFn: (path: string) => api.put(`/series/${seriesId}`, { folder_name: path }),
    onSuccess: invalidate,
  });

  return (
    <div className="folders-panel">
      {data?.map((f) => (
        <div className="folder-line" key={f.id ?? "primary"}>
          📁 <code>{f.path || "(unset)"}</code>
          {f.primary && <span className="tag">primary</span>}
          {!f.exists && <span className="tag" style={{ color: "var(--danger)" }}>missing</span>}
          {f.primary ? (
            <button className="btn sm" onClick={() => setPicking("primary")}>
              Change
            </button>
          ) : (
            <button className="btn sm" title="Remove" onClick={() => removeExtra.mutate(f.id!)}>
              ✕
            </button>
          )}
        </div>
      ))}
      <button className="btn sm" onClick={() => setPicking("add")}>
        + Add folder
      </button>
      {picking && (
        <FolderBrowser
          onPick={(path) => {
            if (picking === "add") addExtra.mutate(path);
            else setPrimary.mutate(path);
            setPicking(null);
          }}
          onClose={() => setPicking(null)}
        />
      )}
    </div>
  );
}

/** Edit a series' source links: remove wrong links, search a source and pick
 *  the right entry, and re-sync the chapter list from the corrected links. */
export function SourcesModal({
  seriesId,
  links,
  onClose,
  onChanged,
}: {
  seriesId: number;
  links: SourceLink[];
  onClose: () => void;
  onChanged: () => void;
}) {
  const queryClient = useQueryClient();
  const invalidate = () => {
    queryClient.invalidateQueries({ queryKey: ["series", seriesId] });
    onChanged();
  };
  const [source, setSource] = useState("");
  const [query, setQuery] = useState("");
  const [results, setResults] = useState<SourceCandidate[] | null>(null);
  const [resyncMsg, setResyncMsg] = useState("");

  const { data: sources } = useQuery({
    queryKey: ["sources"],
    queryFn: () => api.get<string[]>("/sources"),
  });

  const remove = useMutation({
    mutationFn: (id: number) => api.del(`/series/${seriesId}/sources/${id}`),
    onSuccess: invalidate,
  });
  const search = useMutation({
    mutationFn: () =>
      api.get<SourceCandidate[]>(
        `/series/${seriesId}/sources/search?source_name=${source}&query=${encodeURIComponent(query)}`,
      ),
    onSuccess: setResults,
  });
  const setLink = useMutation({
    mutationFn: (c: SourceCandidate) =>
      api.post(`/series/${seriesId}/sources`, {
        source_name: c.source_name,
        external_id: c.external_id,
        external_title: c.title,
        external_url: c.url,
      }),
    onSuccess: () => {
      setResults(null);
      setQuery("");
      invalidate();
    },
  });
  const resync = useMutation({
    mutationFn: () => api.post<{ chapters: number; matched_chapters: number }>(
      `/series/${seriesId}/resync`,
    ),
    onSuccess: (r) => {
      setResyncMsg(`Rebuilt: ${r.chapters} chapters, ${r.matched_chapters} adopted from disk.`);
      invalidate();
    },
  });

  return (
    <Modal title="Edit sources" onClose={onClose}>
      <p className="section-hint">
        Current links. Remove a wrong one, then search the correct source below and pick the
        right entry. After fixing links, re-sync to rebuild the chapter list.
      </p>
      <table className="data-table">
        <tbody>
          {links.length === 0 && (
            <tr><td className="muted">No source links.</td></tr>
          )}
          {links.map((l) => (
            <tr key={l.id}>
              <td><span className="pill blue">{l.source_name}</span></td>
              <td>{l.external_title || l.external_id}</td>
              <td style={{ width: 40 }}>
                <button className="btn sm" title="Remove" onClick={() => remove.mutate(l.id)}>
                  ✕
                </button>
              </td>
            </tr>
          ))}
        </tbody>
      </table>

      <h4 className="files-heading">Add / fix a source</h4>
      <div className="form-row">
        <select value={source} onChange={(e) => setSource(e.target.value)}>
          <option value="">Source…</option>
          {sources?.map((s) => <option key={s} value={s}>{s}</option>)}
        </select>
        <input
          placeholder="Search title…"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && source && query && search.mutate()}
          style={{ flex: 1 }}
        />
        <button className="btn" disabled={!source || !query || search.isPending}
          onClick={() => search.mutate()}>
          Search
        </button>
      </div>
      {search.isError && <div className="error-banner">{(search.error as Error).message}</div>}
      {results && (
        <table className="data-table">
          <tbody>
            {results.length === 0 && <tr><td className="muted">No matches.</td></tr>}
            {results.map((c) => (
              <tr key={c.external_id}>
                <td>{c.title}</td>
                <td style={{ width: 70 }}>
                  <button className="btn sm primary" onClick={() => setLink.mutate(c)}>
                    Use
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      <div style={{ marginTop: 16, display: "flex", gap: 10, alignItems: "center" }}>
        <button className="btn danger" disabled={resync.isPending} onClick={() => resync.mutate()}>
          {resync.isPending ? "Re-syncing…" : "Re-sync chapters from links"}
        </button>
        {resyncMsg && <span style={{ fontSize: 13, color: "var(--success)" }}>{resyncMsg}</span>}
      </div>
    </Modal>
  );
}

/** Interactive duplicate/orphan cleanup: pick which copy to keep per duplicate
 *  group, choose which stray files to delete. Sensible defaults pre-selected. */
export function CleanupModal({
  seriesId,
  onClose,
  onDone,
}: {
  seriesId: number;
  onClose: () => void;
  onDone: () => void;
}) {
  const { data, isLoading, isError, error } = useQuery({
    queryKey: ["cleanup", seriesId],
    queryFn: () => api.get<CleanupPlan>(`/series/${seriesId}/cleanup`),
  });
  // per duplicate group: the path to KEEP; per orphan: whether to DELETE
  const [keepers, setKeepers] = useState<Record<number, string>>({});
  const [orphanDel, setOrphanDel] = useState<Record<string, boolean>>({});
  const [result, setResult] = useState<{ deleted: number; freed_bytes: number; repointed: number; skipped: number } | null>(null);

  useEffect(() => {
    if (!data) return;
    const k: Record<number, string> = {};
    data.groups.forEach((g, i) => {
      k[i] = (g.files.find((f) => f.keep) ?? g.files[0]).path;
    });
    setKeepers(k);
    setOrphanDel(Object.fromEntries(data.orphans.map((o) => [o.path, !o.keep])));
  }, [data]);

  const deletePaths = () => {
    const del: string[] = [];
    data?.groups.forEach((g, i) => {
      g.files.forEach((f) => {
        if (f.path !== keepers[i]) del.push(f.path);
      });
    });
    data?.orphans.forEach((o) => {
      if (orphanDel[o.path]) del.push(o.path);
    });
    return del;
  };

  const apply = useMutation({
    mutationFn: () => api.post<typeof result>(`/series/${seriesId}/cleanup`, { delete: deletePaths() }),
    onSuccess: (r) => {
      setResult(r);
      onDone();
    },
  });

  const toDelete = deletePaths();

  return (
    <Modal title="Clean up files" onClose={onClose}>
      {isLoading ? (
        <Spinner />
      ) : isError ? (
        <div className="error-banner">{(error as Error).message}</div>
      ) : result ? (
        <>
          <p className="section-hint">
            Deleted {result.deleted} file{result.deleted === 1 ? "" : "s"} ({formatBytes(result.freed_bytes)} freed)
            {result.repointed > 0 && `, re-pointed ${result.repointed} chapter(s)`}
            {result.skipped > 0 && `, skipped ${result.skipped}`}.
          </p>
          <div style={{ textAlign: "right" }}>
            <button className="btn primary" onClick={onClose}>Done</button>
          </div>
        </>
      ) : !data || (data.groups.length === 0 && data.orphans.length === 0) ? (
        <p style={{ color: "var(--text-dim)" }}>No duplicates or stray files — nothing to clean up.</p>
      ) : (
        <>
          {data.groups.length > 0 && (
            <>
              <h4 className="files-heading">Duplicates — choose the copy to keep</h4>
              {data.groups.map((g, i) => (
                <div key={g.label} className="cleanup-group">
                  <div className="cleanup-label">{g.label}</div>
                  {g.files.map((f) => (
                    <label key={f.path} className="cleanup-row">
                      <input
                        type="radio"
                        name={`grp${i}`}
                        checked={keepers[i] === f.path}
                        onChange={() => setKeepers({ ...keepers, [i]: f.path })}
                      />
                      <span className={keepers[i] === f.path ? "keep" : "del"}>{f.name}</span>
                      <span className="cleanup-meta">
                        {formatBytes(f.size)}
                        {f.referenced && <span className="tag">in use</span>}
                        {keepers[i] !== f.path && <span className="tag danger-tag">delete</span>}
                      </span>
                    </label>
                  ))}
                </div>
              ))}
            </>
          )}
          {data.orphans.length > 0 && (
            <>
              <h4 className="files-heading">Stray files</h4>
              {data.orphans.map((o) => (
                <label key={o.path} className="cleanup-row">
                  <input
                    type="checkbox"
                    checked={!!orphanDel[o.path]}
                    onChange={(e) => setOrphanDel({ ...orphanDel, [o.path]: e.target.checked })}
                  />
                  <span className={orphanDel[o.path] ? "del" : ""}>{o.name}</span>
                  <span className="cleanup-meta">
                    {formatBytes(o.size)}
                    {orphanDel[o.path] && <span className="tag danger-tag">delete</span>}
                  </span>
                </label>
              ))}
            </>
          )}
          {apply.isError && <div className="error-banner">{(apply.error as Error).message}</div>}
          <div style={{ marginTop: 16, display: "flex", gap: 8, justifyContent: "flex-end" }}>
            <button className="btn" onClick={onClose}>Cancel</button>
            <button
              className="btn danger"
              disabled={apply.isPending || toDelete.length === 0}
              onClick={() => apply.mutate()}
            >
              Delete {toDelete.length} file{toDelete.length === 1 ? "" : "s"}
            </button>
          </div>
        </>
      )}
    </Modal>
  );
}

/** Preview + apply renames into mangarr's naming convention (Sonarr-style). */
export function RenameModal({
  seriesId,
  onClose,
  onDone,
}: {
  seriesId: number;
  onClose: () => void;
  onDone: () => void;
}) {
  const { data, isLoading, isError, error } = useQuery({
    queryKey: ["rename", seriesId],
    queryFn: () => api.get<RenameItem[]>(`/series/${seriesId}/rename`),
  });
  const [outcomes, setOutcomes] = useState<RenameOutcome[] | null>(null);
  // which rows are checked, keyed by row index; default all on when data loads
  const [selected, setSelected] = useState<Set<number>>(new Set());
  useEffect(() => {
    // default-select only the items that can actually be renamed (not conflicts)
    if (data) setSelected(new Set(data.map((_, i) => i).filter((i) => !data[i].conflict)));
  }, [data]);

  const toggle = (i: number) =>
    setSelected((prev) => {
      const next = new Set(prev);
      next.has(i) ? next.delete(i) : next.add(i);
      return next;
    });
  const selectable = (data ?? []).map((_, i) => i).filter((i) => !data![i].conflict);
  const allOn = selectable.length > 0 && selectable.every((i) => selected.has(i));
  const toggleAll = () => setSelected(allOn ? new Set() : new Set(selectable));

  const apply = useMutation({
    mutationFn: () => {
      const chapterIds = (data ?? [])
        .filter((_, i) => selected.has(i))
        .flatMap((item) => item.chapter_ids);
      return api.post<RenameOutcome[]>(`/series/${seriesId}/rename`, {
        chapter_ids: chapterIds,
      });
    },
    onSuccess: (res) => {
      setOutcomes(res);
      onDone();
    },
  });

  return (
    <Modal title="Rename files" onClose={onClose}>
      {isLoading ? (
        <Spinner />
      ) : isError ? (
        <div className="error-banner">{(error as Error).message}</div>
      ) : outcomes ? (
        <>
          <p className="section-hint">
            {outcomes.filter((o) => o.status === "renamed").length} renamed,{" "}
            {outcomes.filter((o) => o.status !== "renamed").length} skipped.
          </p>
          <table className="data-table">
            <tbody>
              {outcomes.map((o, i) => (
                <tr key={i}>
                  <td>{o.status === "renamed" ? "✓" : "⚠"}</td>
                  <td>{o.new_name}</td>
                  <td style={{ color: "var(--text-faint)" }}>
                    {o.status !== "renamed" ? o.status.replace("skipped-", "") : ""}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          <div style={{ marginTop: 14, textAlign: "right" }}>
            <button className="btn primary" onClick={onClose}>Done</button>
          </div>
        </>
      ) : !data || data.length === 0 ? (
        <p style={{ color: "var(--text-dim)" }}>
          Everything already matches your naming convention — nothing to rename.
        </p>
      ) : (
        <>
          <p className="section-hint">
            Select the files to rename (format preserved). {selected.size} of {data.length} selected.
            {data.some((i) => i.conflict) && (
              <>
                {" "}
                <span style={{ color: "var(--danger)" }}>
                  ⚠ some targets already exist (a duplicate file) and can't be renamed.
                </span>
              </>
            )}
          </p>
          <table className="data-table rename-table">
            <thead>
              <tr>
                <th style={{ width: 28 }}>
                  <input type="checkbox" checked={allOn} onChange={toggleAll} title="Select all" />
                </th>
                <th>Current</th>
                <th></th>
                <th>New</th>
              </tr>
            </thead>
            <tbody>
              {data.map((i, idx) => (
                <tr key={idx} className={selected.has(idx) ? "" : "row-off"}>
                  <td>
                    <input
                      type="checkbox"
                      checked={selected.has(idx)}
                      disabled={i.conflict}
                      onChange={() => toggle(idx)}
                    />
                  </td>
                  <td className="old">{i.current_name}</td>
                  <td className="arrow">→</td>
                  <td className="new">
                    {i.new_name}
                    {i.conflict && (
                      <span className="tag" style={{ color: "var(--danger)" }}>already exists</span>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          {apply.isError && (
            <div className="error-banner">{(apply.error as Error).message}</div>
          )}
          <div style={{ marginTop: 14, display: "flex", gap: 8, justifyContent: "flex-end" }}>
            <button className="btn" onClick={onClose}>Cancel</button>
            <button
              className="btn primary"
              disabled={apply.isPending || selected.size === 0}
              onClick={() => apply.mutate()}
            >
              Organize {selected.size} file{selected.size === 1 ? "" : "s"}
            </button>
          </div>
        </>
      )}
    </Modal>
  );
}

/** Two inputs to map a whole-volume archive to a chapter range. */
function RangeMapper({ onMap }: { onMap: (from: number, to: number) => void }) {
  const [from, setFrom] = useState("");
  const [to, setTo] = useState("");
  const valid = from !== "" && to !== "";
  return (
    <div className="range-mapper">
      <input
        type="number"
        placeholder="ch"
        value={from}
        onChange={(e) => setFrom(e.target.value)}
      />
      <span>–</span>
      <input type="number" placeholder="ch" value={to} onChange={(e) => setTo(e.target.value)} />
      <button
        className="btn sm"
        disabled={!valid}
        onClick={() => valid && onMap(Number(from), Number(to))}
      >
        Map range
      </button>
    </div>
  );
}

/** Lists media files found in the series folder; unmatched ones can be
 *  mapped to a chapter (or a chapter range for whole-volume archives). */
export function FilesModal({
  seriesId,
  chapters,
  onClose,
  onChanged,
}: {
  seriesId: number;
  chapters: Chapter[];
  onClose: () => void;
  onChanged: () => void;
}) {
  const queryClient = useQueryClient();
  const { data, isLoading, isError, error } = useQuery({
    queryKey: ["series-files", seriesId],
    queryFn: () => api.get<SeriesFile[]>(`/series/${seriesId}/files`),
  });

  const map = useMutation({
    mutationFn: (args: { file_path: string; chapter_id: number }) =>
      api.post(`/series/${seriesId}/files/map`, args),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["series-files", seriesId] });
      onChanged();
    },
  });

  const mapRange = useMutation({
    mutationFn: (args: { file_path: string; from_number: number; to_number: number }) =>
      api.post(`/series/${seriesId}/files/map-range`, args),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["series-files", seriesId] });
      onChanged();
    },
  });

  // a volume archive is matched by covering chapters, not by being a
  // single chapter — only files covering nothing need manual mapping
  const unmatched =
    data?.filter((f) => f.matched_chapter_id == null && f.covered_count === 0) ?? [];
  const matched =
    data?.filter((f) => f.matched_chapter_id != null || f.covered_count > 0) ?? [];

  return (
    <Modal title="Files on disk" onClose={onClose}>
      {isLoading ? (
        <Spinner />
      ) : isError ? (
        <div className="error-banner">{(error as Error).message}</div>
      ) : !data || data.length === 0 ? (
        <p style={{ color: "var(--text-dim)" }}>No media files found in the series folder.</p>
      ) : (
        <>
          {unmatched.length > 0 && (
            <>
              <h4 className="files-heading">Unmatched ({unmatched.length})</h4>
              <p className="section-hint">
                These files couldn't be matched automatically. Map one to a chapter if it belongs.
              </p>
              <table className="data-table">
                <tbody>
                  {unmatched.map((f) => (
                    <tr key={f.path}>
                      <td>
                        {f.name}
                        {f.volume_number != null && (
                          <span className="tag">vol {f.volume_number}</span>
                        )}
                      </td>
                      <td style={{ width: 180 }}>
                        <select
                          defaultValue=""
                          onChange={(e) =>
                            e.target.value &&
                            map.mutate({ file_path: f.path, chapter_id: Number(e.target.value) })
                          }
                        >
                          <option value="">Map to chapter…</option>
                          {chapters.map((c) => (
                            <option key={c.id} value={c.id}>
                              {chapterLabel(c.number, c.volume)}
                            </option>
                          ))}
                        </select>
                      </td>
                      <td style={{ width: 210 }}>
                        <RangeMapper
                          onMap={(from_number, to_number) =>
                            mapRange.mutate({ file_path: f.path, from_number, to_number })
                          }
                        />
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </>
          )}
          <h4 className="files-heading">Matched ({matched.length})</h4>
          <table className="data-table">
            <tbody>
              {matched.map((f) => (
                <tr key={f.path}>
                  <td>{f.name}</td>
                  <td style={{ color: "var(--text-faint)" }}>
                    {f.chapter_number != null
                      ? `Ch. ${f.chapter_number}`
                      : f.volume_number != null
                      ? `Vol. ${f.volume_number} — covers ${f.covered_count} chapter${
                          f.covered_count === 1 ? "" : "s"
                        }`
                      : ""}
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
