import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useNavigate } from "react-router-dom";
import { api } from "../api/client";
import type { FolderPreview, MetadataResult, MonitorMode, RootFolder } from "../api/types";
import { FolderBrowser } from "./FolderBrowser";
import { MonitorModeFields } from "./SeriesAutomation";
import { Modal, Toggle } from "./common";

/** What adding needs to know about a metadata entry — a search result, a
 * related title, or a matched import folder all provide it. */
export type AddTarget = Pick<
  MetadataResult,
  "provider" | "provider_id" | "title" | "english_title" | "alt_titles"
>;

export function AddSeriesModal({
  result,
  rootFolders,
  onClose,
}: {
  result: AddTarget;
  rootFolders: RootFolder[];
  onClose: () => void;
}) {
  const navigate = useNavigate();
  const queryClient = useQueryClient();

  const [rootFolderId, setRootFolderId] = useState<number>(rootFolders[0].id);
  const [monitored, setMonitored] = useState(true);
  const [monitorMode, setMonitorMode] = useState<MonitorMode>("all");
  const [monitorFrom, setMonitorFrom] = useState("");
  const [searchNow, setSearchNow] = useState(false);
  const [folderName, setFolderName] = useState("");
  const [folderTouched, setFolderTouched] = useState(false);
  const [extraFolders, setExtraFolders] = useState<string[]>([]);
  const [browsing, setBrowsing] = useState<"primary" | "extra" | null>(null);

  const { data: preview, isFetching: previewLoading } = useQuery({
    queryKey: ["folder-preview", rootFolderId, result.provider, result.provider_id],
    queryFn: () =>
      api.post<FolderPreview>("/library/folder-preview", {
        root_folder_id: rootFolderId,
        title: result.title,
        alt_titles: [result.english_title, ...result.alt_titles].filter(Boolean),
      }),
  });

  // autofill the detected folder unless the user already edited it
  useEffect(() => {
    if (preview && !folderTouched) setFolderName(preview.folder_name);
  }, [preview, folderTouched]);

  const addMutation = useMutation({
    mutationFn: () =>
      api.post<{ id: number }>("/series", {
        [result.provider === "anilist" ? "anilist_id" : "mangaupdates_id"]:
          Number(result.provider_id),
        root_folder_id: rootFolderId,
        monitored,
        monitor_mode: monitorMode,
        ...(monitorMode === "from_chapter" ? { monitor_from: Number(monitorFrom) } : {}),
        search_now: searchNow,
        english_title: result.english_title,
        alt_titles: result.alt_titles,
        folder_name: folderName.trim(),
        // an edited/chosen folder is deliberate — pin it so scans can't
        // re-adopt a title-matching existing folder over it
        folder_pinned: folderTouched,
        extra_folders: extraFolders,
      }),
    onSuccess: (series) => {
      queryClient.invalidateQueries({ queryKey: ["series"] });
      // other series' related titles may now be in the library
      queryClient.invalidateQueries({ queryKey: ["related"] });
      navigate(`/series/${series.id}`);
    },
  });

  const rootPath = rootFolders.find((rf) => rf.id === rootFolderId)?.path ?? "";
  const usingDetected = !folderTouched && preview?.matched;

  return (
    <Modal title={`Add — ${result.english_title || result.title}`} onClose={onClose}>
      {result.english_title && result.english_title !== result.title && (
        <div style={{ color: "var(--text-dim)", marginBottom: 12 }}>{result.title}</div>
      )}

      {rootFolders.length > 1 && (
        <div className="form-row">
          <label>Root folder</label>
          <select
            value={rootFolderId}
            onChange={(e) => {
              setRootFolderId(Number(e.target.value));
              setFolderTouched(false); // re-detect under the new root
            }}
          >
            {rootFolders.map((rf) => (
              <option key={rf.id} value={rf.id}>
                {rf.path}
              </option>
            ))}
          </select>
        </div>
      )}

      <div className="form-row">
        <label>Series folder</label>
        <div style={{ display: "flex", gap: 8, flex: 1 }}>
          <input
            style={{ flex: 1 }}
            value={folderName}
            placeholder={previewLoading ? "Detecting…" : ""}
            onChange={(e) => {
              setFolderName(e.target.value);
              setFolderTouched(true);
            }}
          />
          <button className="btn" type="button" onClick={() => setBrowsing("primary")}>
            Browse…
          </button>
        </div>
      </div>
      <div style={{ color: "var(--text-faint)", fontSize: 13, margin: "-6px 0 12px" }}>
        {usingDetected ? (
          <>
            {`Existing folder detected: ${preview!.path} — `}
            <button
              type="button"
              style={{ color: "var(--accent)", padding: 0 }}
              onClick={() => {
                setFolderName(preview!.default_folder_name);
                setFolderTouched(true);
              }}
            >
              create a new folder instead
            </button>
          </>
        ) : (
          <>
            {folderName.startsWith("/")
              ? folderName
              : `${rootPath.replace(/\/$/, "")}/${folderName}` +
                (preview && (preview.matched ? folderName !== preview.folder_name : folderName === preview.folder_name)
                  ? " (will be created)"
                  : "")}
            {folderTouched && preview?.matched && (
              <>
                {" — "}
                <button
                  type="button"
                  style={{ color: "var(--accent)", padding: 0 }}
                  onClick={() => {
                    setFolderName(preview.folder_name);
                    setFolderTouched(false);
                  }}
                >
                  use detected folder
                </button>
              </>
            )}
          </>
        )}
      </div>

      {extraFolders.map((path, i) => (
        <div className="form-row" key={path}>
          <label>{i === 0 ? "Extra folders" : ""}</label>
          <div style={{ display: "flex", gap: 8, flex: 1, alignItems: "center" }}>
            <code style={{ flex: 1, wordBreak: "break-all" }}>{path}</code>
            <button
              className="btn icon-btn"
              type="button"
              title="Remove folder"
              onClick={() => setExtraFolders((prev) => prev.filter((_, j) => j !== i))}
            >
              ✕
            </button>
          </div>
        </div>
      ))}
      <div className="form-row">
        <label></label>
        <button className="btn" type="button" onClick={() => setBrowsing("extra")}>
          + Add another folder
        </button>
      </div>

      <div className="form-row">
        <label>Monitor</label>
        <Toggle label="Monitor series" on={monitored} onChange={setMonitored} />
        <span style={{ color: "var(--text-faint)", fontSize: 13 }}>
          Grab new chapters automatically at each monitor interval
        </span>
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
        <span style={{ color: "var(--text-faint)", fontSize: 13 }}>
          Start fetching missing chapters right after the library scan and metadata refresh
        </span>
      </div>

      {addMutation.isError && (
        <div className="error-banner">{(addMutation.error as Error).message}</div>
      )}

      <div style={{ display: "flex", justifyContent: "flex-end", gap: 8, marginTop: 16 }}>
        <button className="btn" onClick={onClose}>
          Cancel
        </button>
        <button
          className="btn primary"
          disabled={
            addMutation.isPending ||
            (monitored && monitorMode === "from_chapter" &&
              (monitorFrom.trim() === "" || !Number.isFinite(Number(monitorFrom))))
          }
          onClick={() => addMutation.mutate()}
        >
          {addMutation.isPending ? "Adding…" : `Add ${result.english_title || result.title}`}
        </button>
      </div>

      {browsing && (
        <FolderBrowser
          onClose={() => setBrowsing(null)}
          onPick={(path) => {
            if (browsing === "primary") {
              setFolderName(path);
              setFolderTouched(true);
            } else if (!extraFolders.includes(path)) {
              setExtraFolders((prev) => [...prev, path]);
            }
            setBrowsing(null);
          }}
        />
      )}
    </Modal>
  );
}
