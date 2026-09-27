import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { api } from "../api/client";
import type { CalendarData, CalendarExpected, CalendarRelease } from "../api/types";
import { EmptyState, ErrorNotice, Spinner, Toolbar, chapterLabel } from "../components/common";

const RANGE_KEY = "calendar-range";

function loadRange(): number {
  try {
    const stored = Number(localStorage.getItem(RANGE_KEY));
    return [7, 14, 30].includes(stored) ? stored : 14;
  } catch {
    return 14;
  }
}

/** Release dates are calendar days (MangaUpdates has no time of day), so
 * they are grouped by their UTC date rather than shifted into local time. */
function dayKey(iso: string): string {
  return iso.slice(0, 10);
}

function dayLabel(key: string): string {
  const today = new Date();
  const todayKey = new Date(Date.UTC(today.getFullYear(), today.getMonth(), today.getDate()))
    .toISOString()
    .slice(0, 10);
  const diff = Math.round((Date.parse(key) - Date.parse(todayKey)) / 86400000);
  if (diff === 0) return "Today";
  if (diff === 1) return "Tomorrow";
  if (diff === -1) return "Yesterday";
  return new Date(`${key}T12:00:00Z`).toLocaleDateString(undefined, {
    weekday: "short",
    month: "short",
    day: "numeric",
    timeZone: "UTC",
  });
}

function groupByDay<T>(items: T[], dateOf: (item: T) => string): [string, T[]][] {
  const groups = new Map<string, T[]>();
  for (const item of items) {
    const key = dayKey(dateOf(item));
    groups.set(key, [...(groups.get(key) ?? []), item]);
  }
  return [...groups.entries()];
}

function Cover({ url }: { url: string }) {
  return url ? <img className="agenda-cover" src={url} alt="" loading="lazy" /> : <div className="agenda-cover" />;
}

function ExpectedRow({ item }: { item: CalendarExpected }) {
  const next = item.last_number !== null ? Math.floor(item.last_number) + 1 : null;
  return (
    <div className="agenda-row">
      <Cover url={item.cover_url} />
      <div className="agenda-text">
        <Link to={`/series/${item.series_id}`}>{item.series_title}</Link>
        <div className="dim">
          {next !== null ? `Ch. ${next} expected` : "Next chapter expected"} · {item.cadence_label} · last{" "}
          {dayLabel(dayKey(item.last_released_at)).toLowerCase()}
        </div>
      </div>
      {item.overdue ? (
        <span className="pill orange" title="The usual release day has passed without a new chapter">
          Late
        </span>
      ) : (
        !item.monitored && <span className="pill gray">Unmonitored</span>
      )}
    </div>
  );
}

function ReleasedRow({ item }: { item: CalendarRelease }) {
  return (
    <div className="agenda-row">
      <Cover url={item.cover_url} />
      <div className="agenda-text">
        <Link to={`/series/${item.series_id}`}>{item.series_title}</Link>
        <div className="dim">
          {chapterLabel(item.number, item.volume)}
          {item.title && ` — ${item.title}`}
        </div>
      </div>
      {item.downloaded ? (
        <span className="pill green">Downloaded</span>
      ) : item.monitored ? (
        <span className="pill orange">Missing</span>
      ) : (
        <span className="pill gray">Unmonitored</span>
      )}
    </div>
  );
}

export default function Calendar() {
  const [range, setRange] = useState(loadRange);
  const { data, isLoading, error, refetch } = useQuery({
    queryKey: ["calendar", range],
    queryFn: () => api.get<CalendarData>(`/calendar?days_back=${range}&days_ahead=${range}`),
  });

  const released = data ? groupByDay(data.released, (r) => r.released_at) : [];
  const expected = data ? groupByDay(data.expected, (e) => e.expected_at) : [];

  return (
    <>
      <Toolbar title="Calendar">
        <select
          value={range}
          aria-label="Date range"
          onChange={(e) => {
            const next = Number(e.target.value);
            setRange(next);
            try {
              localStorage.setItem(RANGE_KEY, String(next));
            } catch {
              /* storage unavailable */
            }
          }}
        >
          <option value={7}>± 1 week</option>
          <option value={14}>± 2 weeks</option>
          <option value={30}>± 30 days</option>
        </select>
      </Toolbar>
      <div className="content calendar-page">
        <ErrorNotice error={error} retry={() => void refetch()} />
        {isLoading ? (
          <Spinner />
        ) : data && data.released.length === 0 && data.expected.length === 0 ? (
          <EmptyState
            icon="◷"
            title="No releases to show"
            hint="Release dates come from MangaUpdates. Series linked to MangaUpdates show up here once they have a few releases."
          />
        ) : (
          data && (
            <div className="calendar-columns">
              <section>
                <h2 className="calendar-heading">Upcoming</h2>
                <p className="section-hint">
                  Predicted from each ongoing series&apos; usual release rhythm, so treat the day as an
                  estimate.
                </p>
                {expected.length === 0 && <p className="dim">Nothing expected in this range.</p>}
                {expected.map(([day, items]) => (
                  <div className="agenda-day" key={day}>
                    <h3>{dayLabel(day)}</h3>
                    {items.map((item) => (
                      <ExpectedRow key={item.series_id} item={item} />
                    ))}
                  </div>
                ))}
              </section>
              <section>
                <h2 className="calendar-heading">Recently released</h2>
                <p className="section-hint">Chapter release dates reported to MangaUpdates.</p>
                {released.length === 0 && <p className="dim">No releases in this range.</p>}
                {released.map(([day, items]) => (
                  <div className="agenda-day" key={day}>
                    <h3>{dayLabel(day)}</h3>
                    {items.map((item) => (
                      <ReleasedRow key={item.chapter_id} item={item} />
                    ))}
                  </div>
                ))}
              </section>
            </div>
          )
        )}
      </div>
    </>
  );
}
