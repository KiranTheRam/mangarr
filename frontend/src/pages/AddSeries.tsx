import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useNavigate } from "react-router-dom";
import { api } from "../api/client";
import type { MetadataResult, RootFolder } from "../api/types";
import { EmptyState, Spinner, Toggle, Toolbar, statusPill } from "../components/common";

export default function AddSeries() {
  const [query, setQuery] = useState("");
  const [submitted, setSubmitted] = useState("");
  const navigate = useNavigate();
  const queryClient = useQueryClient();

  const { data: rootFolders } = useQuery({
    queryKey: ["rootfolders"],
    queryFn: () => api.get<RootFolder[]>("/rootfolders"),
  });

  const { data: results, isFetching } = useQuery({
    queryKey: ["metadata-search", submitted],
    queryFn: () => api.get<MetadataResult[]>(`/search/metadata?q=${encodeURIComponent(submitted)}`),
    enabled: submitted.length > 1,
  });

  const [rootFolderId, setRootFolderId] = useState<number | null>(null);
  const [monitored, setMonitored] = useState(true);
  const effectiveRoot = rootFolderId ?? rootFolders?.[0]?.id ?? null;

  const addMutation = useMutation({
    mutationFn: (r: MetadataResult) =>
      api.post<{ id: number }>("/series", {
        [r.provider === "anilist" ? "anilist_id" : "mangaupdates_id"]: Number(r.provider_id),
        root_folder_id: effectiveRoot,
        monitored,
        english_title: r.english_title,
        alt_titles: r.alt_titles,
      }),
    onSuccess: (series) => {
      queryClient.invalidateQueries({ queryKey: ["series"] });
      navigate(`/series/${series.id}`);
    },
  });

  return (
    <>
      <Toolbar title="Add New Series" />
      <div className="content">
        <form
          onSubmit={(e) => {
            e.preventDefault();
            setSubmitted(query.trim());
          }}
          style={{ display: "flex", gap: 10, marginBottom: 24, maxWidth: 640 }}
        >
          <input
            autoFocus
            style={{ flex: 1 }}
            placeholder="Search MangaUpdates for a manga title…"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
          />
          <button className="btn primary" type="submit">
            Search
          </button>
        </form>

        {rootFolders && rootFolders.length === 0 && (
          <div className="error-banner">
            No root folder configured — add one in Settings before adding series.
          </div>
        )}

        {rootFolders && rootFolders.length > 1 && (
          <div className="form-row" style={{ maxWidth: 640 }}>
            <label>Root folder</label>
            <select
              value={effectiveRoot ?? ""}
              onChange={(e) => setRootFolderId(Number(e.target.value))}
            >
              {rootFolders.map((rf) => (
                <option key={rf.id} value={rf.id}>
                  {rf.path}
                </option>
              ))}
            </select>
          </div>
        )}

        <div className="form-row" style={{ maxWidth: 640 }}>
          <label>Monitor new series</label>
          <Toggle on={monitored} onChange={setMonitored} />
          <span style={{ color: "var(--text-faint)", fontSize: 13 }}>
            {monitored
              ? "Chapters will be grabbed automatically"
              : "Added to the library only — no automatic downloads"}
          </span>
        </div>

        {addMutation.isError && (
          <div className="error-banner">{(addMutation.error as Error).message}</div>
        )}

        {isFetching ? (
          <Spinner />
        ) : results && results.length === 0 ? (
          <EmptyState icon="🔍" title="No results" hint="Try a different title." />
        ) : (
          results?.map((r) => (
            <div className="search-result" key={r.provider_id}>
              {r.cover_url && <img src={r.cover_url} alt="" />}
              <div style={{ flex: 1 }}>
                <h3>
                  {r.title} {r.year ? <span style={{ color: "var(--text-faint)" }}>({r.year})</span> : null}
                </h3>
                {r.english_title && r.english_title !== r.title && (
                  <div className="alt-title-line">English: {r.english_title}</div>
                )}
                <span className={`pill ${statusPill[r.status] ?? "gray"}`}>{r.status}</span>{" "}
                {r.total_chapters && <span className="tag">{r.total_chapters} chapters</span>}
                {r.genres.slice(0, 4).map((g) => (
                  <span className="tag" key={g}>
                    {g}
                  </span>
                ))}
                <div className="desc" dangerouslySetInnerHTML={{ __html: r.description }} />
              </div>
              <div style={{ alignSelf: "center" }}>
                {r.in_library ? (
                  <span className="pill green">In library</span>
                ) : (
                  <button
                    className="btn primary"
                    disabled={!effectiveRoot || addMutation.isPending}
                    onClick={() => addMutation.mutate(r)}
                  >
                    + Add
                  </button>
                )}
              </div>
            </div>
          ))
        )}
      </div>
    </>
  );
}
