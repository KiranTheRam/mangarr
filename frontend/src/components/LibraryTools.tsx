import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../api/client";
import type {
  Chapter,
  RenameItem,
  RenameOutcome,
  SeriesFile,
  SeriesFolder,
} from "../api/types";
import { Modal, Spinner, chapterLabel } from "./common";
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

  const apply = useMutation({
    mutationFn: () => api.post<RenameOutcome[]>(`/series/${seriesId}/rename`, {}),
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
            {data.length} file{data.length === 1 ? "" : "s"} will be renamed (format preserved):
          </p>
          <table className="data-table rename-table">
            <tbody>
              {data.map((i, idx) => (
                <tr key={idx}>
                  <td className="old">{i.current_name}</td>
                  <td className="arrow">→</td>
                  <td className="new">{i.new_name}</td>
                </tr>
              ))}
            </tbody>
          </table>
          {apply.isError && (
            <div className="error-banner">{(apply.error as Error).message}</div>
          )}
          <div style={{ marginTop: 14, display: "flex", gap: 8, justifyContent: "flex-end" }}>
            <button className="btn" onClick={onClose}>Cancel</button>
            <button className="btn primary" disabled={apply.isPending} onClick={() => apply.mutate()}>
              Organize {data.length} file{data.length === 1 ? "" : "s"}
            </button>
          </div>
        </>
      )}
    </Modal>
  );
}

/** Lists media files found in the series folder; unmatched ones can be
 *  mapped to a chapter manually. */
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

  const unmatched = data?.filter((f) => f.matched_chapter_id == null) ?? [];
  const matched = data?.filter((f) => f.matched_chapter_id != null) ?? [];

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
                      <td>{f.name}</td>
                      <td style={{ width: 200 }}>
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
                      ? `Vol. ${f.volume_number}`
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
