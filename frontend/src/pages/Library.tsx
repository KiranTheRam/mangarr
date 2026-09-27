import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Link, useNavigate } from "react-router-dom";
import { api } from "../api/client";
import type { Series } from "../api/types";
import { LibraryEditorBar } from "../components/LibraryEditor";
import { EmptyState, ErrorNotice, Spinner, Toolbar } from "../components/common";
import { matchesQuery } from "../seriesSearch";

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

/** Decimal chapters (60.5 …) are specials — still searched for, but they never
 * count toward completion, so they get their own tally rather than dragging the
 * main progress below 100%. */
function specialsLabel(s: Series): string | undefined {
  if (s.special_count === 0) return undefined;
  return `${s.special_downloaded_count} of ${s.special_count} special chapter${
    s.special_count === 1 ? "" : "s"
  } downloaded (not counted toward completion)`;
}

/** In selection mode (`selected` set) a click picks the card instead of
 * opening the series; shift-click extends from the last pick. */
function PosterCard({
  series,
  selected,
  onPick,
}: {
  series: Series;
  selected?: boolean;
  onPick?: (shift: boolean) => void;
}) {
  const navigate = useNavigate();
  const pct =
    series.chapter_count > 0 ? (series.downloaded_count / series.chapter_count) * 100 : 0;
  const selecting = selected !== undefined;
  return (
    <div
      className={`poster-card${selecting ? " selecting" : ""}${selected ? " selected" : ""}`}
      onClick={(e) => (onPick ? onPick(e.shiftKey) : navigate(`/series/${series.id}`))}
      {...(onPick && {
        role: "checkbox",
        "aria-checked": selected,
        "aria-label": series.title,
        tabIndex: 0,
        onKeyDown: (e: React.KeyboardEvent) => {
          if (e.key === " " || e.key === "Enter") {
            e.preventDefault();
            onPick(e.shiftKey);
          }
        },
      })}
    >
      {selecting && <div className="poster-check" aria-hidden="true">{selected ? "✓" : ""}</div>}
      {series.cover_url ? (
        <img src={series.cover_url} alt={series.title} loading="lazy" />
      ) : (
        <div className="no-cover">{series.title}</div>
      )}
      <div className={`poster-ribbon${series.monitored ? "" : " unmonitored"}`} />
      <div className="poster-label">
        {series.title}
        <div style={{ fontSize: 11, color: "#bbb", marginTop: 2 }} title={specialsLabel(series)}>
          {series.downloaded_count} / {series.chapter_count || "?"}
          {series.special_count > 0 && (
            <span style={{ color: "#888" }}>
              {" "}
              +{series.special_downloaded_count}/{series.special_count}
            </span>
          )}
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
  // library editor: null when not selecting
  const [selected, setSelected] = useState<Set<number> | null>(null);
  const [lastPicked, setLastPicked] = useState<number | null>(null);
  const { data, isLoading, error, refetch } = useQuery({
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
  // actions only ever touch what's on screen: a series hidden by a filter
  // after being picked is not silently edited or removed
  const selectedVisible = selected ? (filtered ?? []).filter((s) => selected.has(s.id)) : [];

  const pick = (id: number, shift: boolean) => {
    if (!selected || !filtered) return;
    const next = new Set(selected);
    const at = filtered.findIndex((s) => s.id === id);
    const anchor = lastPicked === null ? -1 : filtered.findIndex((s) => s.id === lastPicked);
    if (shift && anchor >= 0 && at >= 0) {
      // extend to the anchor's state across the range, like a file manager
      const on = selected.has(lastPicked!);
      for (const s of filtered.slice(Math.min(anchor, at), Math.max(anchor, at) + 1)) {
        if (on) next.add(s.id);
        else next.delete(s.id);
      }
    } else if (next.has(id)) {
      next.delete(id);
    } else {
      next.add(id);
    }
    setSelected(next);
    setLastPicked(id);
  };
  const stopSelecting = () => {
    setSelected(null);
    setLastPicked(null);
  };

  return (
    <>
      <Toolbar title="Library" className="library-toolbar">
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
          className="toolbar-search"
          placeholder="Search library…"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
        />
        {filtering && data && filtered && (
          <span style={{ fontSize: 12, color: "#999" }}>
            {filtered.length} of {data.length}
          </span>
        )}
        {data && data.length > 0 && (
          <button
            className={`btn${selected ? " active" : ""}`}
            aria-pressed={selected !== null}
            title="Select series to edit, refresh, search or remove several at once"
            onClick={() => (selected ? stopSelecting() : setSelected(new Set()))}
          >
            ☑ Select
          </button>
        )}
        <Link to="/add" className="btn primary">
          + Add Series
        </Link>
      </Toolbar>
      <div className="content">
        <ErrorNotice error={error} retry={() => void refetch()} />
        {isLoading ? (
          <Spinner />
        ) : !data && error ? null : !data || data.length === 0 ? (
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
            {filtered.map((s) =>
              selected ? (
                <PosterCard
                  key={s.id}
                  series={s}
                  selected={selected.has(s.id)}
                  onPick={(shift) => pick(s.id, shift)}
                />
              ) : (
                <PosterCard key={s.id} series={s} />
              ),
            )}
          </div>
        )}
      </div>
      {selected && (
        <LibraryEditorBar
          selected={selectedVisible}
          visibleCount={filtered?.length ?? 0}
          onSelectAll={() => setSelected(new Set(filtered?.map((s) => s.id)))}
          onClear={() => {
            setSelected(new Set());
            setLastPicked(null);
          }}
          onDone={stopSelecting}
        />
      )}
    </>
  );
}
