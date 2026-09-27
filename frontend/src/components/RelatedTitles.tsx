import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { useNavigate } from "react-router-dom";
import { api } from "../api/client";
import type { RelatedData, RelatedTitle, RootFolder } from "../api/types";
import { AddSeriesModal } from "./AddSeriesModal";
import { ErrorNotice } from "./common";

const OPEN_KEY = "related-open";

function loadOpen(): boolean {
  try {
    return localStorage.getItem(OPEN_KEY) === "true";
  } catch {
    return false;
  }
}

const FORMAT_LABELS: Record<string, string> = { ONE_SHOT: "One-shot", MANGA: "" };

function RelatedCard({ item, onAdd }: { item: RelatedTitle; onAdd: (item: RelatedTitle) => void }) {
  const navigate = useNavigate();
  const owned = item.in_library_series_id !== null;
  const format = FORMAT_LABELS[item.format] ?? "";
  return (
    <button
      type="button"
      className="related-card"
      title={owned ? "Open in library" : "Add to library"}
      onClick={() => (owned ? navigate(`/series/${item.in_library_series_id}`) : onAdd(item))}
    >
      {item.cover_url ? (
        <img src={item.cover_url} alt="" loading="lazy" />
      ) : (
        <div className="related-no-cover">{item.title}</div>
      )}
      <span className="related-relation">
        {item.relation}
        {format && ` · ${format}`}
      </span>
      <span className="related-title">{item.title}</span>
      {owned ? <span className="pill green">In library</span> : <span className="related-add">+ Add</span>}
    </button>
  );
}

/** Sequels, side stories and similar series, fetched only once opened. */
export function RelatedTitles({ seriesId }: { seriesId: number }) {
  const [open, setOpen] = useState(loadOpen);
  const [adding, setAdding] = useState<RelatedTitle | null>(null);
  const { data, isFetching, error, refetch } = useQuery({
    queryKey: ["related", seriesId],
    queryFn: () => api.get<RelatedData>(`/series/${seriesId}/related`),
    enabled: open,
    staleTime: 10 * 60 * 1000,
  });
  const { data: rootFolders } = useQuery({
    queryKey: ["rootfolders"],
    queryFn: () => api.get<RootFolder[]>("/rootfolders"),
    enabled: open,
  });

  const toggle = () => {
    setOpen(!open);
    try {
      localStorage.setItem(OPEN_KEY, String(!open));
    } catch {
      /* storage unavailable */
    }
  };

  const empty = data && data.relations.length === 0 && data.recommendations.length === 0;
  return (
    <div className="related-section">
      <button type="button" className="related-toggle" aria-expanded={open} onClick={toggle}>
        <span className="chevron">{open ? "▾" : "▸"}</span> Related &amp; recommended
        {data && !empty && (
          <span className="dim"> · {data.relations.length + data.recommendations.length}</span>
        )}
      </button>
      {open && (
        <>
          <ErrorNotice error={error} retry={() => void refetch()} />
          {isFetching && !data && (
            <div className="dim related-loading">
              <span className="mini-spinner" /> Asking AniList and MangaUpdates…
            </div>
          )}
          {empty && <p className="dim">No related titles or recommendations found.</p>}
          {data && data.relations.length > 0 && (
            <>
              <h4 className="related-heading">Related</h4>
              <div className="related-strip">
                {data.relations.map((item) => (
                  <RelatedCard key={`${item.provider}:${item.provider_id}`} item={item} onAdd={setAdding} />
                ))}
              </div>
            </>
          )}
          {data && data.recommendations.length > 0 && (
            <>
              <h4 className="related-heading">Readers also like</h4>
              <div className="related-strip">
                {data.recommendations.map((item) => (
                  <RelatedCard key={`${item.provider}:${item.provider_id}`} item={item} onAdd={setAdding} />
                ))}
              </div>
            </>
          )}
        </>
      )}
      {adding && rootFolders && rootFolders.length > 0 && (
        <AddSeriesModal result={adding} rootFolders={rootFolders} onClose={() => setAdding(null)} />
      )}
    </div>
  );
}
