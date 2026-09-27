import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../api/client";
import type { MonitorMode, RootFolder, Series, SeriesEditorResult } from "../api/types";
import { MONITOR_MODES } from "./SeriesAutomation";
import { ErrorNotice, Modal } from "./common";

function seriesCount(n: number): string {
  return `${n} series`;
}

/** Titles for a confirmation, capped so a huge selection stays readable. */
function TitleList({ series }: { series: Series[] }) {
  const shown = series.slice(0, 8);
  return (
    <ul className="editor-title-list">
      {shown.map((s) => (
        <li key={s.id}>{s.title}</li>
      ))}
      {series.length > shown.length && <li className="more">and {series.length - shown.length} more</li>}
    </ul>
  );
}

/** One change applied to every selected series; fields left on "No change"
 * aren't touched. */
function EditSeriesModal({ series, onClose }: { series: Series[]; onClose: () => void }) {
  const queryClient = useQueryClient();
  const [monitored, setMonitored] = useState<"" | "true" | "false">("");
  const [mode, setMode] = useState<"" | MonitorMode>("");
  const [rootId, setRootId] = useState("");
  const [moveFiles, setMoveFiles] = useState(true);

  const { data: rootFolders } = useQuery({
    queryKey: ["rootfolders"],
    queryFn: () => api.get<RootFolder[]>("/rootfolders"),
  });

  const save = useMutation({
    mutationFn: () =>
      api.put<SeriesEditorResult>("/series/editor", {
        series_ids: series.map((s) => s.id),
        monitored: monitored === "" ? null : monitored === "true",
        monitor_mode: mode || null,
        root_folder_id: rootId ? Number(rootId) : null,
        move_files: moveFiles,
      }),
    onSuccess: (result) => {
      void queryClient.invalidateQueries({ queryKey: ["series"] });
      if (result.problems.length === 0) onClose();
    },
  });

  const changed = monitored !== "" || mode !== "" || rootId !== "";
  const result = save.data;
  const modeHint = MONITOR_MODES.find((m) => m.value === mode)?.hint;

  if (result && result.problems.length > 0) {
    return (
      <Modal title={`Edit ${seriesCount(series.length)}`} onClose={onClose}>
        <p>
          Updated {seriesCount(result.updated)}
          {result.moved > 0 && ` (${result.moved} moved)`}. These stayed in their old root folder:
        </p>
        <ul className="editor-problems">
          {result.problems.map((p) => (
            <li key={p.series_id}>
              <strong>{p.title}</strong> — {p.detail}
            </li>
          ))}
        </ul>
        <div className="modal-actions">
          <button className="btn primary" onClick={onClose}>Close</button>
        </div>
      </Modal>
    );
  }

  return (
    <Modal title={`Edit ${seriesCount(series.length)}`} onClose={onClose}>
      <div className="form-row">
        <label htmlFor="editor-monitored">Monitoring</label>
        <select
          id="editor-monitored"
          value={monitored}
          onChange={(e) => setMonitored(e.target.value as typeof monitored)}
        >
          <option value="">No change</option>
          <option value="true">Monitored</option>
          <option value="false">Unmonitored</option>
        </select>
      </div>
      <div className="form-row">
        <label htmlFor="editor-mode">Chapters to monitor</label>
        <select id="editor-mode" value={mode} onChange={(e) => setMode(e.target.value as typeof mode)}>
          <option value="">No change</option>
          {/* a chapter threshold only makes sense for one series at a time */}
          {MONITOR_MODES.filter((m) => m.value !== "from_chapter").map((m) => (
            <option key={m.value} value={m.value}>
              {m.label}
            </option>
          ))}
        </select>
      </div>
      {mode && (
        <p className="form-hint">
          {modeHint} Applying a mode resets per-chapter monitoring choices in these series.
        </p>
      )}
      <div className="form-row">
        <label htmlFor="editor-root">Root folder</label>
        <select id="editor-root" value={rootId} onChange={(e) => setRootId(e.target.value)}>
          <option value="">No change</option>
          {rootFolders?.map((f) => (
            <option key={f.id} value={f.id}>
              {f.path}
            </option>
          ))}
        </select>
      </div>
      {rootId && (
        <>
          <label className="editor-check">
            <input type="checkbox" checked={moveFiles} onChange={(e) => setMoveFiles(e.target.checked)} />
            Move existing files into the new root folder
          </label>
          <p className="form-hint">
            {moveFiles
              ? "Each series' folder moves as a whole. Series with downloads in progress, or whose folder name is already taken there, are skipped and listed afterwards. Extra folders outside the series folder stay where they are."
              : "Existing files stay where they are and remain tracked; new chapters are saved in the new root folder."}
          </p>
        </>
      )}
      <ErrorNotice error={save.error} />
      <div className="modal-actions">
        <button className="btn" onClick={onClose}>Cancel</button>
        <button className="btn primary" disabled={!changed || save.isPending} onClick={() => save.mutate()}>
          {save.isPending ? (rootId && moveFiles ? "Moving…" : "Saving…") : "Apply"}
        </button>
      </div>
    </Modal>
  );
}

function DeleteSeriesModal({
  series,
  onClose,
  onDeleted,
}: {
  series: Series[];
  onClose: () => void;
  onDeleted: () => void;
}) {
  const queryClient = useQueryClient();
  const remove = useMutation({
    mutationFn: () => api.post("/series/editor/delete", { series_ids: series.map((s) => s.id) }),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["series"] });
      onDeleted();
    },
  });
  return (
    <Modal title={`Remove ${seriesCount(series.length)}`} onClose={onClose}>
      <p>Remove these from Mangarr? Files on disk are kept.</p>
      <TitleList series={series} />
      <ErrorNotice error={remove.error} />
      <div className="modal-actions">
        <button className="btn" onClick={onClose}>Cancel</button>
        <button className="btn danger" disabled={remove.isPending} onClick={() => remove.mutate()}>
          {remove.isPending ? "Removing…" : `Remove ${seriesCount(series.length)}`}
        </button>
      </div>
    </Modal>
  );
}

/** Actions for the series selected on the library page. */
export function LibraryEditorBar({
  selected,
  visibleCount,
  onSelectAll,
  onClear,
  onDone,
}: {
  selected: Series[];
  visibleCount: number;
  onSelectAll: () => void;
  onClear: () => void;
  onDone: () => void;
}) {
  const queryClient = useQueryClient();
  const [dialog, setDialog] = useState<"edit" | "delete" | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  const refresh = useMutation({
    mutationFn: (searchMissing: boolean) =>
      api.post<{ count: number }>("/series/editor/refresh", {
        series_ids: selected.map((s) => s.id),
        search_missing: searchMissing,
      }),
    onSuccess: ({ count }, searchMissing) => {
      void queryClient.invalidateQueries({ queryKey: ["series"] });
      const message = searchMissing
        ? `Refreshing ${seriesCount(count)} and searching for missing monitored chapters, one at a time.`
        : `Refreshing ${seriesCount(count)} in the background, one at a time.`;
      setNotice(message);
      window.setTimeout(() => setNotice((current) => (current === message ? null : current)), 8000);
    },
  });

  const none = selected.length === 0;
  return (
    <div className="editor-bar" role="region" aria-label="Library editor">
      <div className="editor-bar-selection">
        <strong>{selected.length}</strong> of {visibleCount} selected
        {selected.length < visibleCount ? (
          <button className="btn sm" onClick={onSelectAll}>Select all</button>
        ) : (
          <button className="btn sm" onClick={onClear}>Select none</button>
        )}
      </div>
      {(notice || refresh.error) && (
        <div className={`editor-bar-notice${refresh.error ? " error" : ""}`} role="status">
          {refresh.error ? (refresh.error as Error).message : notice}
        </div>
      )}
      <div className="editor-bar-actions">
        <button className="btn" disabled={none} onClick={() => setDialog("edit")}>
          ✎ Edit
        </button>
        <button
          className="btn"
          disabled={none || refresh.isPending}
          title="Refresh metadata and chapter lists"
          onClick={() => refresh.mutate(false)}
        >
          ↻ Refresh
        </button>
        <button
          className="btn"
          disabled={none || refresh.isPending}
          title="Refresh, then queue every missing chapter the series' monitoring wants (unmonitored series grab nothing)"
          onClick={() => refresh.mutate(true)}
        >
          <span>🔍 Search<span className="wide-only"> missing</span></span>
        </button>
        <button className="btn danger" disabled={none} onClick={() => setDialog("delete")}>
          ✕ Remove
        </button>
        {/* phones leave it out for room; the highlighted Select button exits too */}
        <button className="btn primary editor-bar-done" onClick={onDone}>
          Done
        </button>
      </div>
      {dialog === "edit" && <EditSeriesModal series={selected} onClose={() => setDialog(null)} />}
      {dialog === "delete" && (
        <DeleteSeriesModal
          series={selected}
          onClose={() => setDialog(null)}
          onDeleted={() => {
            setDialog(null);
            onClear();
          }}
        />
      )}
    </div>
  );
}
