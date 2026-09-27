import type { Series } from "./api/types";

/** Every name we know for the series — canonical (often romaji/Japanese),
 * English, and all alt titles (including native-script ones) — lowercased. */
function namesOf(series: Series): string[] {
  return [series.title, series.english_title, ...series.alt_titles.split("\n")]
    .filter(Boolean)
    .map((name) => name.toLowerCase());
}

/** Case-insensitive match against every name of the series, so both
 * "kagura" and "カグラバチ" find it. `q` must already be lowercased. */
export function matchesQuery(series: Series, q: string): boolean {
  return namesOf(series).some((name) => name.includes(q));
}

/** How well a name matches: whole name, then prefix, then the start of a
 * word, then anywhere. Lower is better; null is no match. */
function matchRank(name: string, q: string): number | null {
  if (name === q) return 0;
  if (name.startsWith(q)) return 1;
  const at = name.indexOf(q);
  if (at < 0) return null;
  return /[\s\-:.,!?'"(（「]/.test(name[at - 1]) ? 2 : 3;
}

/** Library series matching `q` (lowercased), best matches first; ties keep
 * library order. */
export function rankSeries(series: Series[], q: string, limit: number): Series[] {
  const ranked: { series: Series; rank: number; index: number }[] = [];
  series.forEach((s, index) => {
    let best: number | null = null;
    for (const name of namesOf(s)) {
      const rank = matchRank(name, q);
      if (rank !== null && (best === null || rank < best)) best = rank;
    }
    if (best !== null) ranked.push({ series: s, rank: best, index });
  });
  ranked.sort((a, b) => a.rank - b.rank || a.index - b.index);
  return ranked.slice(0, limit).map((r) => r.series);
}
