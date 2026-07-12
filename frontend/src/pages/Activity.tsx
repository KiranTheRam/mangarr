import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../api/client";
import type { HistoryItem, QueueItem } from "../api/types";
import { EmptyState, Spinner, statusPill, Toolbar } from "../components/common";

function Queue() {
  const queryClient = useQueryClient();
  const [selected, setSelected] = useState<Set<number>>(() => new Set());
  const { data, isLoading } = useQuery({
    queryKey: ["queue"],
    queryFn: () => api.get<QueueItem[]>("/queue"),
    refetchInterval: 2000,
  });

  const remove = useMutation({
    mutationFn: (id: number) => api.del(`/queue/${id}`),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["queue"] }),
  });

  const removeSelected = useMutation({
    mutationFn: (ids: number[]) => api.post("/queue/remove", { ids }),
    onSuccess: () => {
      setSelected(new Set());
      queryClient.invalidateQueries({ queryKey: ["queue"] });
    },
  });

  if (isLoading) return <Spinner />;
  if (!data || data.length === 0)
    return <EmptyState icon="⇅" title="Queue is empty" hint="Grabbed releases will appear here." />;

  // only ids still in the queue count (items can finish between refetches)
  const selectedVisible = data.filter((item) => selected.has(item.id)).map((item) => item.id);
  const allSelected = selectedVisible.length === data.length;

  const toggle = (id: number) => {
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  };

  return (
    <>
      <div className="table-actions" style={{ display: "flex", alignItems: "center", gap: 12, marginBottom: 10 }}>
        <button
          className="btn"
          onClick={() => setSelected(allSelected ? new Set() : new Set(data.map((i) => i.id)))}
        >
          {allSelected ? "Clear selected" : "Select all"}
        </button>
        <span>{selectedVisible.length} selected</span>
        <button
          className="btn danger"
          disabled={selectedVisible.length === 0 || removeSelected.isPending}
          onClick={() => removeSelected.mutate(selectedVisible)}
        >
          {removeSelected.isPending ? "Removing…" : "Remove selected"}
        </button>
      </div>
      <table className="data-table card-table queue-table">
        <thead>
          <tr>
            <th style={{ width: 34 }}>
              <input
                type="checkbox"
                checked={allSelected}
                onChange={() => setSelected(allSelected ? new Set() : new Set(data.map((i) => i.id)))}
              />
            </th>
            <th>Title</th>
            <th style={{ width: 110 }}>Source</th>
            <th style={{ width: 90 }}>Type</th>
            <th style={{ width: 110 }}>Status</th>
            <th style={{ width: 180 }}>Progress</th>
            <th style={{ width: 60 }}></th>
          </tr>
        </thead>
        <tbody>
          {data.map((item) => (
            <tr key={item.id}>
              <td className="cell-select">
                <input
                  type="checkbox"
                  checked={selected.has(item.id)}
                  onChange={() => toggle(item.id)}
                />
              </td>
              <td className="cell-qtitle">
                {item.title || item.series_title}
                {item.status === "failed" && item.error && (
                  <div style={{ color: "var(--danger)", fontSize: "0.85em", marginTop: 2 }}>
                    {item.error}
                  </div>
                )}
              </td>
              <td className="cell-source">{item.source_name}</td>
              <td className="cell-type">
                <span className={`pill ${item.kind === "torrent" ? "orange" : "blue"}`}>
                  {item.kind}
                </span>
              </td>
              <td className="cell-status">
                <span className={`pill ${statusPill[item.status] ?? "gray"}`}>{item.status}</span>
              </td>
              <td className="cell-progress">
                <div className="progress-bar">
                  <div style={{ width: `${Math.round(item.progress * 100)}%` }} />
                  <span>{Math.round(item.progress * 100)}%</span>
                </div>
              </td>
              <td className="cell-remove">
                <button
                  className="btn icon-btn"
                  title="Remove"
                  disabled={remove.isPending}
                  onClick={() => remove.mutate(item.id)}
                >
                  ✕
                </button>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </>
  );
}

function History() {
  const { data, isLoading } = useQuery({
    queryKey: ["history"],
    queryFn: () => api.get<HistoryItem[]>("/history"),
    refetchInterval: 5000,
  });

  if (isLoading) return <Spinner />;
  if (!data || data.length === 0) return <EmptyState icon="🕘" title="No history yet" />;

  return (
    <table className="data-table card-table history-table">
      <thead>
        <tr>
          <th style={{ width: 100 }}>Event</th>
          <th style={{ width: 220 }}>Series</th>
          <th>Detail</th>
          <th style={{ width: 110 }}>Source</th>
          <th style={{ width: 170 }}>Date</th>
        </tr>
      </thead>
      <tbody>
        {data.map((ev) => (
          <tr key={ev.id}>
            <td className="cell-event">
              <span className={`pill ${statusPill[ev.event] ?? "gray"}`}>{ev.event}</span>
            </td>
            <td className="cell-series">{ev.series_title}</td>
            <td className="cell-detail" style={{ color: "var(--text-dim)", wordBreak: "break-all" }}>{ev.detail}</td>
            <td className="cell-source">{ev.source_name}</td>
            <td className="cell-date" style={{ color: "var(--text-dim)" }}>
              {new Date(ev.created_at).toLocaleString()}
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

export default function Activity() {
  const [tab, setTab] = useState<"queue" | "history">("queue");
  return (
    <>
      <Toolbar title="Activity">
        <button className={`btn${tab === "queue" ? " primary" : ""}`} onClick={() => setTab("queue")}>
          Queue
        </button>
        <button
          className={`btn${tab === "history" ? " primary" : ""}`}
          onClick={() => setTab("history")}
        >
          History
        </button>
      </Toolbar>
      <div className="content">{tab === "queue" ? <Queue /> : <History />}</div>
    </>
  );
}
