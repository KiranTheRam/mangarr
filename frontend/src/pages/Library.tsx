import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Link, useNavigate } from "react-router-dom";
import { api } from "../api/client";
import type { Series } from "../api/types";
import { EmptyState, Spinner, Toolbar } from "../components/common";

/** Case-insensitive match against every name we know for the series —
 * canonical (often romaji/Japanese), English, and all alt titles (including
 * native-script ones), so both "kagura" and "カグラバチ" find it. */
function matchesQuery(series: Series, q: string): boolean {
  return [series.title, series.english_title, series.alt_titles]
    .join("\n")
    .toLowerCase()
    .includes(q);
}

interface Filters {
  monitored: "all" | "monitored" | "unmonitored";
  status: "all" | "ongoing" | "finished";
  content: "all" | "missing" | "complete";
}

const NO_FILTERS: Filters = { monitored: "all", status: "all", content: "all" };
const FILTERS_KEY = "library-filters";

function loadFilters(): Filters {
  try {
    return { ...NO_FILTERS, ...JSON.parse(localStorage.getItem(FILTERS_KEY) ?? "{}") };
  } catch {
    return NO_FILTERS;
  }
}

function matchesFilters(s: Series, f: Filters): boolean {
  if (f.monitored !== "all" && s.monitored !== (f.monitored === "monitored")) return false;
  // finished/cancelled mean no more content is coming; everything else
  // (releasing, hiatus, not yet released, unknown) counts as ongoing
  const finished = s.status === "finished" || s.status === "cancelled";
  if (f.status !== "all" && finished !== (f.status === "finished")) return false;
  const missing = s.downloaded_count < s.chapter_count;
  if (f.content !== "all" && missing !== (f.content === "missing")) return false;
  return true;
}

function PosterCard({ series }: { series: Series }) {
  const navigate = useNavigate();
  const pct =
    series.chapter_count > 0 ? (series.downloaded_count / series.chapter_count) * 100 : 0;
  return (
    <div className="poster-card" onClick={() => navigate(`/series/${series.id}`)}>
      {series.cover_url ? (
        <img src={series.cover_url} alt={series.title} loading="lazy" />
      ) : (
        <div className="no-cover">{series.title}</div>
      )}
      <div className={`poster-ribbon${series.monitored ? "" : " unmonitored"}`} />
      <div className="poster-label">
        {series.title}
        <div style={{ fontSize: 11, color: "#bbb", marginTop: 2 }}>
          {series.downloaded_count} / {series.chapter_count || "?"}
        </div>
      </div>
      <div className="poster-progress">
        <div className={pct < 100 ? "partial" : ""} style={{ width: `${pct}%` }} />
      </div>
    </div>
  );
}

export default function Library() {
  const [query, setQuery] = useState("");
  const [filters, setFilters] = useState<Filters>(loadFilters);
  const { data, isLoading } = useQuery({
    queryKey: ["series"],
    queryFn: () => api.get<Series[]>("/series"),
  });

  const setFilter = (key: keyof Filters) => (e: React.ChangeEvent<HTMLSelectElement>) => {
    const next = { ...filters, [key]: e.target.value };
    setFilters(next);
    localStorage.setItem(FILTERS_KEY, JSON.stringify(next));
  };

  const q = query.trim().toLowerCase();
  const filtered = data
    ?.filter((s) => matchesFilters(s, filters))
    .filter((s) => !q || matchesQuery(s, q));
  const filtering = q || filters.monitored !== "all" || filters.status !== "all" || filters.content !== "all";

  return (
    <>
      <Toolbar title="Library">
        <select value={filters.monitored} onChange={setFilter("monitored")}>
          <option value="all">All</option>
          <option value="monitored">Monitored</option>
          <option value="unmonitored">Unmonitored</option>
        </select>
        <select value={filters.status} onChange={setFilter("status")}>
          <option value="all">Any status</option>
          <option value="ongoing">Ongoing</option>
          <option value="finished">Finished</option>
        </select>
        <select value={filters.content} onChange={setFilter("content")}>
          <option value="all">Any content</option>
          <option value="missing">Missing chapters</option>
          <option value="complete">All downloaded</option>
        </select>
        <input
          type="search"
          placeholder="Search library…"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          style={{ width: 260 }}
        />
        {filtering && data && filtered && (
          <span style={{ fontSize: 12, color: "#999" }}>
            {filtered.length} of {data.length}
          </span>
        )}
        <Link to="/add" className="btn primary">
          + Add Series
        </Link>
      </Toolbar>
      <div className="content">
        {isLoading ? (
          <Spinner />
        ) : !data || data.length === 0 ? (
          <EmptyState
            icon="📚"
            title="Your library is empty"
            hint="Add a series to start building your manga collection."
          />
        ) : !filtered || filtered.length === 0 ? (
          <EmptyState
            icon="🔍"
            title="No matches"
            hint={
              q
                ? `Nothing in your library matches “${query.trim()}”.`
                : "No series match the current filters."
            }
          />
        ) : (
          <div className="poster-grid">
            {filtered.map((s) => (
              <PosterCard key={s.id} series={s} />
            ))}
          </div>
        )}
      </div>
    </>
  );
}
