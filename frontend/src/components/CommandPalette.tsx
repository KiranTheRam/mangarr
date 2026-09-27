import { useEffect, useId, useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { useNavigate } from "react-router-dom";
import { api } from "../api/client";
import type { Series } from "../api/types";
import { rankSeries } from "../seriesSearch";

const PAGES = [
  { to: "/", label: "Library", keywords: "home series" },
  { to: "/add", label: "Add new series", keywords: "search mangaupdates" },
  { to: "/add/import", label: "Library import", keywords: "existing folders bulk" },
  { to: "/add/lists", label: "Import lists", keywords: "anilist myanimelist mal mangadex mangaupdates" },
  { to: "/calendar", label: "Calendar", keywords: "upcoming releases schedule" },
  { to: "/activity", label: "Activity", keywords: "queue history downloads" },
  { to: "/wanted", label: "Wanted", keywords: "missing chapters" },
  { to: "/settings", label: "Settings", keywords: "sources kavita qbittorrent root folders" },
];

const MAX_SERIES = 8;

export const paletteShortcut = /Mac|iPhone|iPad/.test(navigator.platform) ? "⌘K" : "Ctrl K";

interface Item {
  key: string;
  to: string;
  series?: Series;
  label: string;
  hint?: string;
}

function itemsFor(series: Series[] | undefined, query: string): Item[] {
  const q = query.trim().toLowerCase();
  const items: Item[] = [];
  for (const s of q && series ? rankSeries(series, q, MAX_SERIES) : []) {
    items.push({ key: `s${s.id}`, to: `/series/${s.id}`, series: s, label: s.title });
  }
  for (const page of PAGES) {
    if (!q || `${page.label} ${page.keywords}`.toLowerCase().includes(q)) {
      items.push({ key: page.to, to: page.to, label: page.label, hint: "Go to" });
    }
  }
  if (q.length > 1) {
    items.push({
      key: "add",
      to: `/add?q=${encodeURIComponent(query.trim())}`,
      label: `Search MangaUpdates for “${query.trim()}”`,
      hint: "Add",
    });
  }
  return items;
}

function SeriesRow({ series }: { series: Series }) {
  const english = series.english_title && series.english_title !== series.title ? series.english_title : "";
  return (
    <>
      {series.cover_url ? (
        <img className="palette-cover" src={series.cover_url} alt="" loading="lazy" />
      ) : (
        <span className="palette-cover" />
      )}
      <span className="palette-text">
        <span className="palette-label">{series.title}</span>
        {english && <span className="palette-sub">{english}</span>}
      </span>
      <span className="palette-hint">
        {series.downloaded_count}/{series.chapter_count || "?"}
        {!series.monitored && " · unmonitored"}
      </span>
    </>
  );
}

/** Jump to any series or page from anywhere: Ctrl/⌘+K opens it, arrows move,
 * Enter goes. */
export default function CommandPalette({
  open,
  setOpen,
}: {
  open: boolean;
  setOpen: (open: boolean) => void;
}) {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && !e.altKey && e.key.toLowerCase() === "k") {
        e.preventDefault();
        setOpen(!open);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, setOpen]);

  return open ? <Palette onClose={() => setOpen(false)} /> : null;
}

function Palette({ onClose }: { onClose: () => void }) {
  const navigate = useNavigate();
  const dialogRef = useRef<HTMLDialogElement>(null);
  const listRef = useRef<HTMLUListElement>(null);
  const listId = useId();
  const [query, setQuery] = useState("");
  const [active, setActive] = useState(0);

  // shares the library page's cache, so it's usually instant
  const { data: series, isLoading } = useQuery({
    queryKey: ["series"],
    queryFn: () => api.get<Series[]>("/series"),
  });

  useEffect(() => {
    const dialog = dialogRef.current!;
    const previousFocus = document.activeElement;
    dialog.showModal();
    return () => {
      dialog.close();
      if (previousFocus instanceof HTMLElement && previousFocus.isConnected) previousFocus.focus();
    };
  }, []);

  const items = itemsFor(series, query);
  const current = Math.min(active, items.length - 1);

  useEffect(() => {
    listRef.current?.querySelector(`[data-index="${current}"]`)?.scrollIntoView({ block: "nearest" });
  }, [current]);

  const go = (item: Item | undefined) => {
    if (!item) return;
    onClose();
    navigate(item.to);
  };

  const onKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      e.preventDefault();
      const step = e.key === "ArrowDown" ? 1 : -1;
      setActive((current + step + items.length) % Math.max(items.length, 1));
    } else if (e.key === "Enter") {
      e.preventDefault();
      go(items[current]);
    }
  };

  const optionId = (i: number) => `${listId}-${i}`;
  return (
    <dialog
      ref={dialogRef}
      className="modal-backdrop palette-backdrop"
      aria-label="Go to series or page"
      onCancel={(e) => {
        e.preventDefault();
        onClose();
      }}
      onClick={(e) => {
        if (e.target === e.currentTarget) onClose();
      }}
    >
      <div className="palette">
        <input
          autoFocus
          className="palette-input"
          type="text"
          role="combobox"
          aria-expanded="true"
          aria-controls={listId}
          aria-activedescendant={items.length > 0 ? optionId(current) : undefined}
          placeholder="Jump to a series or page…"
          value={query}
          onChange={(e) => {
            setQuery(e.target.value);
            setActive(0);
          }}
          onKeyDown={onKeyDown}
        />
        <ul className="palette-list" id={listId} role="listbox" ref={listRef}>
          {items.map((item, i) => (
            <li
              key={item.key}
              id={optionId(i)}
              data-index={i}
              role="option"
              aria-selected={i === current}
              className={`palette-item${i === current ? " active" : ""}`}
              onMouseMove={() => i !== current && setActive(i)}
              onClick={() => go(item)}
            >
              {item.series ? (
                <SeriesRow series={item.series} />
              ) : (
                <>
                  <span className="palette-text">
                    <span className="palette-label">{item.label}</span>
                  </span>
                  <span className="palette-hint">{item.hint}</span>
                </>
              )}
            </li>
          ))}
        </ul>
        {query.trim() && isLoading && <div className="palette-empty">Loading library…</div>}
        <div className="palette-footer">
          <span>↑↓ to move</span>
          <span>↵ to open</span>
          <span>esc to close</span>
        </div>
      </div>
    </dialog>
  );
}
