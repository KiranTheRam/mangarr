import { useEffect, useRef, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { api } from "../api/client";
import type {
  ImportFolder,
  ImportMatch,
  LibraryImportResult,
  MetadataResult,
  MonitorMode,
  RootFolder,
} from "../api/types";
import { MonitorModeFields } from "../components/SeriesAutomation";
import { EmptyState, ErrorNotice, Modal, Spinner, Toggle, Toolbar, statusPill } from "../components/common";
import { AddTabs } from "./AddSeries";

interface Row {
  folder: ImportFolder;
  status: "pending" | "matching" | "done" | "error";
  candidates: MetadataResult[];
  choice: MetadataResult | null;
  selected: boolean;
  error?: string;
}

function displayTitle(r: MetadataResult): string {
  return r.english_title || r.title;
}

function MatchPicker({
  row,
  onPick,
  onClose,
}: {
  row: Row;
  onPick: (choice: MetadataResult) => void;
  onClose: () => void;
}) {
  const [query, setQuery] = useState(row.folder.query);
  const [provider, setProvider] = useState("mangaupdates");
  const [submitted, setSubmitted] = useState<{ query: string; provider: string } | null>(null);
  const { data, isFetching, error } = useQuery({
    queryKey: ["import-match", submitted?.provider, submitted?.query],
    queryFn: () =>
      api.get<ImportMatch>(
        `/library/import/match?provider=${submitted!.provider}&query=${encodeURIComponent(submitted!.query)}`,
      ),
    enabled: !!submitted,
  });
  // until the user searches, offer what the automatic match already found
  const candidates = submitted ? data?.candidates ?? [] : row.candidates;

  return (
    <Modal title={`Match — ${row.folder.name}`} onClose={onClose}>
      <form
        className="import-picker-search"
        onSubmit={(e) => {
          e.preventDefault();
          if (query.trim()) setSubmitted({ query: query.trim(), provider });
        }}
      >
        <input autoFocus value={query} onChange={(e) => setQuery(e.target.value)} aria-label="Search title" />
        <select value={provider} onChange={(e) => setProvider(e.target.value)} aria-label="Metadata provider">
          <option value="mangaupdates">MangaUpdates</option>
          <option value="anilist">AniList</option>
        </select>
        <button className="btn primary" type="submit">
          Search
        </button>
      </form>
      <ErrorNotice error={error} />
      {isFetching ? (
        <Spinner />
      ) : candidates.length === 0 ? (
        <p className="form-hint">No results — try another title or provider.</p>
      ) : (
        <div className="import-picker-list">
          {candidates.map((c) => (
            <button
              type="button"
              key={`${c.provider}:${c.provider_id}`}
              className="import-candidate"
              disabled={c.in_library}
              onClick={() => onPick(c)}
            >
              {c.cover_url ? <img src={c.cover_url} alt="" /> : <div className="import-cover-empty" />}
              <span className="import-candidate-text">
                <strong>
                  {displayTitle(c)} {c.year ? <span className="dim">({c.year})</span> : null}
                </strong>
                {c.english_title && c.english_title !== c.title && <span className="dim">{c.title}</span>}
                <span>
                  {c.status !== "unknown" && (
                    <span className={`pill ${statusPill[c.status] ?? "gray"}`}>{c.status}</span>
                  )}{" "}
                  {c.total_chapters ? <span className="tag">{c.total_chapters} ch</span> : null}
                  {c.in_library && <span className="pill green">In library</span>}
                </span>
              </span>
            </button>
          ))}
        </div>
      )}
    </Modal>
  );
}

function ImportRow({
  row,
  onToggle,
  onChoose,
}: {
  row: Row;
  onToggle: (selected: boolean) => void;
  onChoose: () => void;
}) {
  const c = row.choice;
  const selectable = !!c && !c.in_library;
  return (
    <div className={`import-row${row.selected ? " selected" : ""}`}>
      <input
        type="checkbox"
        checked={row.selected}
        disabled={!selectable}
        aria-label={`Import ${row.folder.name}`}
        onChange={(e) => onToggle(e.target.checked)}
      />
      <div className="import-folder">
        <div className="import-folder-name" title={row.folder.path}>
          {row.folder.name}
        </div>
        <div className="dim">
          {row.folder.file_count} file{row.folder.file_count === 1 ? "" : "s"}
        </div>
      </div>
      <div className="import-match">
        {row.status === "pending" && <span className="dim">Waiting…</span>}
        {row.status === "matching" && (
          <span className="dim">
            <span className="mini-spinner" /> Matching…
          </span>
        )}
        {row.status === "error" && <span className="danger-text">{row.error}</span>}
        {row.status === "done" && !c && <span className="dim">No confident match</span>}
        {c && (
          <>
            {c.cover_url ? <img src={c.cover_url} alt="" /> : <div className="import-cover-empty" />}
            <div className="import-match-text">
              <div>
                {displayTitle(c)} {c.year ? <span className="dim">({c.year})</span> : null}
              </div>
              <div>
                {/* MangaUpdates search results don't carry a status */}
                {c.status !== "unknown" && (
                  <span className={`pill ${statusPill[c.status] ?? "gray"}`}>{c.status}</span>
                )}{" "}
                <span className="dim">{c.provider === "anilist" ? "AniList" : "MangaUpdates"}</span>{" "}
                {c.in_library && <span className="pill green">In library</span>}
              </div>
            </div>
          </>
        )}
      </div>
      <button
        className="btn sm"
        type="button"
        disabled={row.status === "pending" || row.status === "matching"}
        onClick={onChoose}
      >
        {c ? "Change" : "Choose…"}
      </button>
    </div>
  );
}

export default function LibraryImport() {
  const queryClient = useQueryClient();
  const { data: rootFolders, error: foldersError } = useQuery({
    queryKey: ["rootfolders"],
    queryFn: () => api.get<RootFolder[]>("/rootfolders"),
  });
  const [rootFolderId, setRootFolderId] = useState<number | null>(null);
  useEffect(() => {
    if (rootFolderId === null && rootFolders && rootFolders.length > 0) setRootFolderId(rootFolders[0].id);
  }, [rootFolders, rootFolderId]);

  const [rows, setRows] = useState<Row[] | null>(null);
  const [scanning, setScanning] = useState(false);
  const [scanError, setScanError] = useState<unknown>(null);
  const [picking, setPicking] = useState<Row | null>(null);
  const [monitored, setMonitored] = useState(true);
  const [monitorMode, setMonitorMode] = useState<MonitorMode>("all");
  const [monitorFrom, setMonitorFrom] = useState("");
  const [searchNow, setSearchNow] = useState(false);
  const [importing, setImporting] = useState(false);
  const [importError, setImportError] = useState<unknown>(null);
  const [results, setResults] = useState<LibraryImportResult[] | null>(null);
  // bumped by every scan (and on unmount) so a stale matching loop stops
  const runId = useRef(0);
  useEffect(() => () => void runId.current++, []);

  const update = (path: string, patch: Partial<Row>) =>
    setRows((prev) => prev?.map((r) => (r.folder.path === path ? { ...r, ...patch } : r)) ?? prev);

  async function scan() {
    if (rootFolderId === null) return;
    const id = ++runId.current;
    setScanning(true);
    setScanError(null);
    setResults(null);
    let folders: ImportFolder[];
    try {
      folders = await api.get<ImportFolder[]>(`/library/import/folders?root_folder_id=${rootFolderId}`);
    } catch (e) {
      setScanError(e);
      return;
    } finally {
      setScanning(false);
    }
    if (runId.current !== id) return;
    setRows(folders.map((folder) => ({ folder, status: "pending", candidates: [], choice: null, selected: false })));
    // one folder at a time: the metadata providers allow about a request a second
    for (const folder of folders) {
      if (runId.current !== id) return;
      update(folder.path, { status: "matching" });
      try {
        const match = await api.get<ImportMatch>(
          `/library/import/match?query=${encodeURIComponent(folder.query)}`,
        );
        if (runId.current !== id) return;
        const choice = match.best !== null ? match.candidates[match.best] : null;
        update(folder.path, {
          status: "done",
          candidates: match.candidates,
          choice,
          selected: !!choice && !choice.in_library,
        });
      } catch (e) {
        if (runId.current !== id) return;
        update(folder.path, { status: "error", error: e instanceof Error ? e.message : "Match failed" });
      }
    }
  }

  async function runImport() {
    if (!rows || rootFolderId === null) return;
    const chosen = rows.filter((r) => r.selected && r.choice && !r.choice.in_library);
    setImporting(true);
    setImportError(null);
    try {
      const out = await api.post<LibraryImportResult[]>("/library/import", {
        root_folder_id: rootFolderId,
        monitored,
        monitor_mode: monitorMode,
        ...(monitorMode === "from_chapter" ? { monitor_from: Number(monitorFrom) } : {}),
        search_now: searchNow,
        items: chosen.map((r) => ({
          folder_name: r.folder.name,
          provider: r.choice!.provider,
          provider_id: Number(r.choice!.provider_id),
          title: r.choice!.title,
          english_title: r.choice!.english_title,
          alt_titles: r.choice!.alt_titles,
        })),
      });
      setResults(out);
      const handled = new Set(out.filter((r) => r.status !== "failed").map((r) => r.folder_name));
      setRows((prev) => prev?.filter((r) => !handled.has(r.folder.name)) ?? prev);
      queryClient.invalidateQueries({ queryKey: ["series"] });
    } catch (e) {
      setImportError(e);
    } finally {
      setImporting(false);
    }
  }

  const matchingDone = rows?.every((r) => r.status === "done" || r.status === "error") ?? true;
  const matched = rows?.filter((r) => r.choice).length ?? 0;
  const selectedCount = rows?.filter((r) => r.selected).length ?? 0;
  const pendingCount = rows?.filter((r) => r.status === "pending" || r.status === "matching").length ?? 0;
  const setAll = (selected: boolean) =>
    setRows((prev) => prev?.map((r) => ({ ...r, selected: selected && !!r.choice && !r.choice.in_library })) ?? prev);
  const fromInvalid =
    monitored && monitorMode === "from_chapter" && (monitorFrom.trim() === "" || !Number.isFinite(Number(monitorFrom)));

  return (
    <>
      <Toolbar title="Add New Series" />
      <div className="content">
        <AddTabs />
        <p className="section-hint import-intro">
          Add series for folders already in a root folder. Each folder is matched to a MangaUpdates
          entry by name; check the matches, fix any that are wrong, then import them together. The
          files stay where they are.
        </p>
        <ErrorNotice error={foldersError} />
        {rootFolders && rootFolders.length === 0 ? (
          <div className="error-banner">No root folder configured — add one in Settings first.</div>
        ) : (
          <div className="import-controls">
            {rootFolders && rootFolders.length > 1 && (
              <select
                value={rootFolderId ?? ""}
                onChange={(e) => {
                  runId.current++;
                  setRows(null);
                  setRootFolderId(Number(e.target.value));
                }}
                aria-label="Root folder"
              >
                {rootFolders.map((rf) => (
                  <option key={rf.id} value={rf.id}>
                    {rf.path}
                  </option>
                ))}
              </select>
            )}
            {rootFolders && rootFolders.length === 1 && <code>{rootFolders[0].path}</code>}
            <button className="btn primary" disabled={rootFolderId === null || scanning} onClick={() => void scan()}>
              {scanning ? "Scanning…" : rows ? "Rescan" : "Scan folder"}
            </button>
          </div>
        )}
        <ErrorNotice error={scanError} />

        {results && (
          <div className="import-results" role="status">
            <strong>
              Added {results.filter((r) => r.status === "added").length} series
            </strong>
            {results.some((r) => r.status === "exists") &&
              ` · ${results.filter((r) => r.status === "exists").length} already in the library`}
            {results.some((r) => r.status === "failed") &&
              ` · ${results.filter((r) => r.status === "failed").length} failed`}
            <span className="dim"> — chapters are fetched in the background, one series at a time.</span>
            {results
              .filter((r) => r.status === "failed")
              .map((r) => (
                <div key={r.folder_name} className="danger-text">
                  {r.folder_name}: {r.detail}
                </div>
              ))}
            {results.some((r) => r.series_id) && (
              <div className="import-result-links">
                {results
                  .filter((r) => r.series_id)
                  .map((r) => (
                    <Link key={r.folder_name} to={`/series/${r.series_id}`}>
                      {r.folder_name}
                    </Link>
                  ))}
              </div>
            )}
          </div>
        )}

        {scanning ? (
          <Spinner />
        ) : rows && rows.length === 0 ? (
          <EmptyState
            icon="✓"
            title="Nothing to import"
            hint="Every folder in this root folder already belongs to a series."
          />
        ) : (
          rows && (
            <>
              <div className="import-summary">
                <span>
                  {matched} of {rows.length} folder{rows.length === 1 ? "" : "s"} matched
                  {!matchingDone && ` · matching ${rows.length - pendingCount + 1} of ${rows.length}…`}
                </span>
                <button className="btn sm" type="button" onClick={() => setAll(true)}>
                  Select matched
                </button>
                <button className="btn sm" type="button" onClick={() => setAll(false)}>
                  Select none
                </button>
              </div>
              <div className="import-list">
                {rows.map((row) => (
                  <ImportRow
                    key={row.folder.path}
                    row={row}
                    onToggle={(selected) => update(row.folder.path, { selected })}
                    onChoose={() => setPicking(row)}
                  />
                ))}
              </div>

              <div className="settings-section import-options">
                <h3>Import options</h3>
                <div className="form-row">
                  <label>Monitor</label>
                  <Toggle label="Monitor imported series" on={monitored} onChange={setMonitored} />
                </div>
                {monitored && (
                  <MonitorModeFields
                    mode={monitorMode}
                    from={monitorFrom}
                    onMode={setMonitorMode}
                    onFrom={setMonitorFrom}
                  />
                )}
                <div className="form-row">
                  <label>Search for missing content</label>
                  <Toggle label="Search for missing content" on={searchNow} onChange={setSearchNow} />
                </div>
                <ErrorNotice error={importError} />
                <div className="modal-actions">
                  <button
                    className="btn primary"
                    disabled={importing || selectedCount === 0 || fromInvalid}
                    onClick={() => void runImport()}
                  >
                    {importing ? "Importing…" : `Import ${selectedCount} series`}
                  </button>
                </div>
              </div>
            </>
          )
        )}
      </div>
      {picking && (
        <MatchPicker
          row={picking}
          onClose={() => setPicking(null)}
          onPick={(choice) => {
            update(picking.folder.path, { choice, status: "done", selected: !choice.in_library });
            setPicking(null);
          }}
        />
      )}
    </>
  );
}
