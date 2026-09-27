import { useEffect, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { NavLink, useSearchParams } from "react-router-dom";
import { api } from "../api/client";
import type { MetadataResult, RootFolder } from "../api/types";
import { AddSeriesModal } from "../components/AddSeriesModal";
import { EmptyState, ErrorNotice, Spinner, Toolbar, statusPill } from "../components/common";
import { sanitizeDescription } from "../sanitize";

/** The ways to add series: one at a time, a whole existing library, or
 * from reading lists on other sites. */
export function AddTabs() {
  const tabs = [
    { to: "/add", label: "Search" },
    { to: "/add/import", label: "Library import" },
    { to: "/add/lists", label: "Import lists" },
  ];
  return (
    <nav className="source-tabs add-tabs" aria-label="Ways to add series">
      {tabs.map((t) => (
        <NavLink
          key={t.to}
          to={t.to}
          end
          className={({ isActive }) => `source-tab${isActive ? " active" : ""}`}
        >
          {t.label}
        </NavLink>
      ))}
    </nav>
  );
}

export default function AddSeries() {
  // ?q= starts a search right away (the command palette's "search MangaUpdates")
  const [params] = useSearchParams();
  const linkedQuery = params.get("q") ?? "";
  const [query, setQuery] = useState(linkedQuery);
  const [submitted, setSubmitted] = useState(linkedQuery.trim());
  // a new ?q= while already on this page (no remount) replaces the search
  useEffect(() => {
    if (linkedQuery) {
      setQuery(linkedQuery);
      setSubmitted(linkedQuery.trim());
    }
  }, [linkedQuery]);
  const [adding, setAdding] = useState<MetadataResult | null>(null);

  const { data: rootFolders, error: foldersError, refetch: retryFolders } = useQuery({
    queryKey: ["rootfolders"],
    queryFn: () => api.get<RootFolder[]>("/rootfolders"),
  });

  const { data: results, isFetching, error: searchError, refetch: retrySearch } = useQuery({
    queryKey: ["metadata-search", submitted],
    queryFn: () => api.get<MetadataResult[]>(`/search/metadata?q=${encodeURIComponent(submitted)}`),
    enabled: submitted.length > 1,
  });

  const canAdd = !!rootFolders && rootFolders.length > 0;

  return (
    <>
      <Toolbar title="Add New Series" />
      <div className="content">
        <AddTabs />
        <ErrorNotice error={foldersError} retry={() => void retryFolders()} />
        <ErrorNotice error={searchError} retry={() => void retrySearch()} />
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

        {isFetching ? (
          <Spinner />
        ) : results && results.length === 0 ? (
          <EmptyState icon="🔍" title="No results" hint="Try a different title." />
        ) : (
          results?.map((r) => {
            const clickable = canAdd && !r.in_library;
            return (
              <div
                className="search-result"
                key={r.provider_id}
                style={clickable ? { cursor: "pointer" } : undefined}
                title={clickable ? "Add this series" : undefined}
                onClick={clickable ? () => setAdding(r) : undefined}
                role={clickable ? "button" : undefined}
                tabIndex={clickable ? 0 : undefined}
                aria-label={clickable ? `Add ${r.english_title || r.title}` : undefined}
                onKeyDown={clickable ? (event) => {
                  if (event.key === "Enter" || event.key === " ") { event.preventDefault(); setAdding(r); }
                } : undefined}
              >
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
                  <div
                    className="desc"
                    dangerouslySetInnerHTML={{ __html: sanitizeDescription(r.description) }}
                  />
                </div>
                {r.in_library && (
                  <div style={{ alignSelf: "center" }}>
                    <span className="pill green">In library</span>
                  </div>
                )}
              </div>
            );
          })
        )}
      </div>

      {adding && rootFolders && rootFolders.length > 0 && (
        <AddSeriesModal
          result={adding}
          rootFolders={rootFolders}
          onClose={() => setAdding(null)}
        />
      )}
    </>
  );
}
