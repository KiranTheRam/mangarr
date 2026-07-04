import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { api } from "../api/client";
import type { FilesystemList } from "../api/types";
import { Modal, Spinner } from "./common";

/** Sonarr-style folder picker constrained to the configured root folders. */
export function FolderBrowser({
  onPick,
  onClose,
}: {
  onPick: (path: string) => void;
  onClose: () => void;
}) {
  const [path, setPath] = useState("");
  const { data, isLoading, isError, error } = useQuery({
    queryKey: ["filesystem", path],
    queryFn: () =>
      api.get<FilesystemList>(`/filesystem${path ? `?path=${encodeURIComponent(path)}` : ""}`),
  });

  return (
    <Modal title="Choose a folder" onClose={onClose}>
      <div className="path-bar">
        <code>{data?.path || "Root folders"}</code>
        {data?.path && (
          <button className="btn primary sm" onClick={() => onPick(data.path)}>
            Use this folder
          </button>
        )}
      </div>
      {isError && <div className="error-banner">{(error as Error).message}</div>}
      {isLoading ? (
        <Spinner />
      ) : (
        <ul className="fs-list">
          {data?.parent != null && (
            <li className="fs-row up" onClick={() => setPath(data.parent!)}>
              ⬑ ..
            </li>
          )}
          {data?.entries.length === 0 && (
            <li className="fs-row muted">No sub-folders</li>
          )}
          {data?.entries.map((e) => (
            <li key={e.path} className="fs-row" onClick={() => setPath(e.path)}>
              📁 {e.name}
            </li>
          ))}
        </ul>
      )}
    </Modal>
  );
}
