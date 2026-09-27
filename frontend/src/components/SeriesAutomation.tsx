import { useState } from "react";
import { useMutation, useQuery } from "@tanstack/react-query";
import { api } from "../api/client";
import type { MonitorMode, SeriesDetail, SeriesGroup } from "../api/types";
import { sourceLabel } from "../sources";
import { ErrorNotice, Modal, Spinner, Toggle } from "./common";

export const MONITOR_MODES: { value: MonitorMode; label: string; hint: string }[] = [
  { value: "all", label: "All chapters", hint: "Every chapter, past and future." },
  {
    value: "missing",
    label: "Missing chapters",
    hint: "Chapters without a file now, plus new ones. Files you already have are left alone (never upgraded).",
  },
  {
    value: "future",
    label: "Future chapters",
    hint: "Only chapters newer than the latest one known right now.",
  },
  {
    value: "from_chapter",
    label: "From a chapter onward",
    hint: "The chosen chapter and everything after it.",
  },
  {
    value: "latest_volume",
    label: "Latest volume onward",
    hint: "The newest volume and every chapter not yet collected in a volume.",
  },
  {
    value: "none",
    label: "None",
    hint: "Nothing is grabbed, but the chapter list keeps refreshing so new chapters show up.",
  },
];

export function monitorModeLabel(mode: MonitorMode, from: number | null): string {
  const label = MONITOR_MODES.find((m) => m.value === mode)?.label ?? mode;
  return mode === "from_chapter" && from != null ? `From Ch. ${from}` : label;
}

/** Mode picker plus the chapter field "from a chapter onward" needs. */
export function MonitorModeFields({
  mode,
  from,
  onMode,
  onFrom,
}: {
  mode: MonitorMode;
  from: string;
  onMode: (mode: MonitorMode) => void;
  onFrom: (from: string) => void;
}) {
  const hint = MONITOR_MODES.find((m) => m.value === mode)?.hint;
  return (
    <>
      <div className="form-row">
        <label htmlFor="monitor-mode">Chapters to monitor</label>
        <select id="monitor-mode" value={mode} onChange={(e) => onMode(e.target.value as MonitorMode)}>
          {MONITOR_MODES.map((m) => (
            <option key={m.value} value={m.value}>{m.label}</option>
          ))}
        </select>
      </div>
      {mode === "from_chapter" && (
        <div className="form-row">
          <label htmlFor="monitor-from">First chapter</label>
          <input
            id="monitor-from"
            type="text"
            inputMode="decimal"
            value={from}
            placeholder="e.g. 120"
            onChange={(e) => onFrom(e.target.value)}
          />
        </div>
      )}
      {hint && <p className="form-hint">{hint}</p>}
    </>
  );
}

function linesOf(text: string): string[] {
  return text.split("\n").map((line) => line.trim()).filter(Boolean);
}

function sameOrder(a: string[], b: string[]): boolean {
  return a.length === b.length && a.every((name, i) => name === b[i]);
}

export function AutomationModal({
  series,
  onClose,
  onSaved,
}: {
  series: SeriesDetail;
  onClose: () => void;
  onSaved: () => void;
}) {
  const globalOrder = series.global_source_order;
  const [mode, setMode] = useState<MonitorMode>(series.monitor_mode);
  const [from, setFrom] = useState(series.monitor_from != null ? String(series.monitor_from) : "");
  const [order, setOrder] = useState<string[]>(() => {
    const custom = series.source_priority.filter((name) => globalOrder.includes(name));
    return [...custom, ...globalOrder.filter((name) => !custom.includes(name))];
  });
  const [blocked, setBlocked] = useState<Set<string>>(() => new Set(series.blocked_sources));
  const [preferredGroups, setPreferredGroups] = useState(series.preferred_groups.join("\n"));
  const [blockedGroups, setBlockedGroups] = useState(series.blocked_groups.join("\n"));
  const [upgrades, setUpgrades] = useState(series.upgrades_enabled);
  const [cutoff, setCutoff] = useState(series.upgrade_cutoff);
  const [mergeVolumes, setMergeVolumes] = useState(series.merge_volumes);
  const [showGroups, setShowGroups] = useState(false);

  const modeChanged =
    mode !== series.monitor_mode ||
    (mode === "from_chapter" && Number(from) !== series.monitor_from);
  const fromInvalid = mode === "from_chapter" && (from.trim() === "" || !Number.isFinite(Number(from)));

  const groups = useQuery({
    queryKey: ["series-groups", series.id],
    queryFn: () => api.get<SeriesGroup[]>(`/series/${series.id}/groups`),
    enabled: showGroups,
    staleTime: 5 * 60_000,
  });

  const save = useMutation({
    mutationFn: () =>
      api.put(`/series/${series.id}`, {
        // re-sending the mode re-applies it to every chapter, which resets
        // per-chapter toggles — only do that when it actually changed
        ...(modeChanged ? {
          monitor_mode: mode,
          ...(mode === "from_chapter" ? { monitor_from: Number(from) } : {}),
        } : {}),
        source_priority: sameOrder(order, globalOrder) ? [] : order,
        blocked_sources: order.filter((name) => blocked.has(name)),
        preferred_groups: linesOf(preferredGroups),
        blocked_groups: linesOf(blockedGroups),
        upgrades_enabled: upgrades,
        upgrade_cutoff: cutoff,
        merge_volumes: mergeVolumes,
      }),
    onSuccess: () => {
      onSaved();
      onClose();
    },
  });

  const mergeNow = useMutation({
    mutationFn: () => api.post<{ merged: number[] }>(`/series/${series.id}/volumes/merge`),
    onSuccess: onSaved,
  });

  const move = (index: number, delta: number) => {
    const target = index + delta;
    if (target < 0 || target >= order.length) return;
    const next = [...order];
    [next[index], next[target]] = [next[target], next[index]];
    setOrder(next);
  };

  const addGroup = (text: string, setText: (value: string) => void, group: string) => {
    const lines = linesOf(text);
    if (!lines.some((line) => line.toLowerCase() === group.toLowerCase())) {
      setText([...lines, group].join("\n"));
    }
  };

  return (
    <Modal title={`Automation — ${series.title}`} onClose={onClose}>
      <div className="automation-section">
        <h4>Monitoring</h4>
        <MonitorModeFields mode={mode} from={from} onMode={setMode} onFrom={setFrom} />
        {modeChanged && (
          <p className="form-hint warn">
            Saving applies this mode to every chapter and replaces individual chapter monitor toggles.
          </p>
        )}
      </div>

      <div className="automation-section">
        <h4>Sources</h4>
        <p className="form-hint">
          Order in which grabs try sources for this series. Switch a source off to never grab
          from it here; it still supplies chapter numbers and volume data.
        </p>
        {order.length === 0 ? (
          <p className="form-hint">No download sources are enabled in Settings.</p>
        ) : (
          <div className="priority-list">
            {order.map((name, i) => (
              <div className={`priority-row${blocked.has(name) ? " disabled" : ""}`} key={name}>
                <span className="priority-rank">{i + 1}</span>
                <span className="priority-arrows">
                  <button className="btn icon-btn" disabled={i === 0} onClick={() => move(i, -1)} title="Higher priority">
                    ↑
                  </button>
                  <button
                    className="btn icon-btn"
                    disabled={i === order.length - 1}
                    onClick={() => move(i, 1)}
                    title="Lower priority"
                  >
                    ↓
                  </button>
                </span>
                <span className="priority-name">{sourceLabel(name)}</span>
                <span className="priority-hint">
                  {series.source_links.some((l) => l.source_name === name) ? "" : "not linked"}
                </span>
                <span className="priority-toggle">
                  <span>Grab</span>
                  <Toggle
                    on={!blocked.has(name)}
                    label={`${blocked.has(name) ? "Allow" : "Block"} grabs from ${sourceLabel(name)}`}
                    onChange={(allowed) => {
                      const next = new Set(blocked);
                      if (allowed) next.delete(name);
                      else next.add(name);
                      setBlocked(next);
                    }}
                  />
                </span>
              </div>
            ))}
          </div>
        )}
        {!sameOrder(order, globalOrder) && (
          <button className="btn sm automation-inline-btn" onClick={() => setOrder(globalOrder)}>
            Reset to the global order
          </button>
        )}
      </div>

      <div className="automation-section">
        <h4>Scanlation groups</h4>
        <p className="form-hint">
          One group per line. Preferred groups win in the order listed; blocked groups are never
          grabbed. Applies to sources that carry several groups (MangaDex, Atsumaru).
        </p>
        <div className="form-row top">
          <label htmlFor="preferred-groups">Preferred</label>
          <textarea
            id="preferred-groups"
            rows={3}
            value={preferredGroups}
            onChange={(e) => setPreferredGroups(e.target.value)}
          />
        </div>
        <div className="form-row top">
          <label htmlFor="blocked-groups">Blocked</label>
          <textarea
            id="blocked-groups"
            rows={3}
            value={blockedGroups}
            onChange={(e) => setBlockedGroups(e.target.value)}
          />
        </div>
        {!showGroups ? (
          <button className="btn sm automation-inline-btn" onClick={() => setShowGroups(true)}>
            Show groups from linked sources
          </button>
        ) : groups.isLoading ? (
          <Spinner />
        ) : groups.error ? (
          <ErrorNotice error={groups.error} retry={() => void groups.refetch()} />
        ) : (groups.data ?? []).length === 0 ? (
          <p className="form-hint">No linked source reports scanlation groups for this series.</p>
        ) : (
          <table className="data-table group-table">
            <thead>
              <tr>
                <th>Group</th>
                <th>Source</th>
                <th>Chapters</th>
                <th></th>
              </tr>
            </thead>
            <tbody>
              {groups.data!.map((g) => (
                <tr key={`${g.source_name}-${g.group}`}>
                  <td>{g.group}</td>
                  <td>{sourceLabel(g.source_name)}</td>
                  <td>{g.chapters}</td>
                  <td className="group-actions">
                    <button className="btn sm" onClick={() => addGroup(preferredGroups, setPreferredGroups, g.group)}>
                      Prefer
                    </button>
                    <button className="btn sm" onClick={() => addGroup(blockedGroups, setBlockedGroups, g.group)}>
                      Block
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>

      <div className="automation-section">
        <h4>Upgrades</h4>
        <div className="form-row">
          <label>Upgrade downloads</label>
          <Toggle label="Upgrade downloaded chapters" on={upgrades} onChange={setUpgrades} />
        </div>
        {upgrades && (
          <div className="form-row">
            <label htmlFor="upgrade-cutoff">Stop upgrading at</label>
            <select id="upgrade-cutoff" value={cutoff} onChange={(e) => setCutoff(e.target.value)}>
              <option value="">The top source in the list</option>
              {order.map((name) => (
                <option key={name} value={name}>{sourceLabel(name)} or better</option>
              ))}
            </select>
          </div>
        )}
        <p className="form-hint">
          Re-grabs a chapter when a source higher in the list above has it, or when its file is from
          a blocked group. Only chapter files Mangarr downloaded itself are replaced; your own files,
          torrent imports and volume archives are never touched.
        </p>
      </div>

      <div className="automation-section">
        <h4>Volumes</h4>
        <div className="form-row">
          <label>Merge finished volumes</label>
          <Toggle label="Merge finished volumes" on={mergeVolumes} onChange={setMergeVolumes} />
          <button
            className="btn sm"
            disabled={mergeNow.isPending}
            title="Merge every volume that is complete now, whether or not automatic merging is on"
            onClick={() => mergeNow.mutate()}
          >
            {mergeNow.isPending ? "Merging…" : "Merge now"}
          </button>
        </div>
        {mergeNow.data && (
          <p className="form-hint" role="status">
            {mergeNow.data.merged.length === 0
              ? "No volume is ready to merge."
              : `Merged volume${mergeNow.data.merged.length === 1 ? "" : "s"} ${mergeNow.data.merged.join(", ")}.`}
          </p>
        )}
        <ErrorNotice error={mergeNow.error} />
        <p className="form-hint">
          Once a later volume starts (or the series finishes) and every chapter of a volume is
          downloaded, its chapter files are packed into one "Vol. NN" archive. Volumes containing
          files you added yourself are skipped.
        </p>
      </div>

      <ErrorNotice error={save.error} />
      <div className="modal-actions">
        <button className="btn" onClick={onClose}>Cancel</button>
        <button className="btn primary" disabled={save.isPending || fromInvalid} onClick={() => save.mutate()}>
          {save.isPending ? "Saving…" : "Save"}
        </button>
      </div>
    </Modal>
  );
}
