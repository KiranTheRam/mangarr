import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../api/client";
import type { HistoryItem, QueueItem } from "../api/types";
import { EmptyState, Spinner, statusPill, Toolbar } from "../components/common";

function Queue() {
  const queryClient = useQueryClient();
  const { data, isLoading } = useQuery({
    queryKey: ["queue"],
    queryFn: () => api.get<QueueItem[]>("/queue"),
    refetchInterval: 3000,
  });

  const remove = useMutation({
    mutationFn: (id: number) => api.del(`/queue/${id}`),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["queue"] }),
  });

  if (isLoading) return <Spinner />;
  if (!data || data.length === 0)
    return <EmptyState icon="⇅" title="Queue is empty" hint="Grabbed releases will appear here." />;

  return (
    <table className="data-table">
      <thead>
        <tr>
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
            <td>{item.title || item.series_title}</td>
            <td>{item.source_name}</td>
            <td>
              <span className={`pill ${item.kind === "torrent" ? "orange" : "blue"}`}>
                {item.kind}
              </span>
            </td>
            <td>
              <span className={`pill ${statusPill[item.status] ?? "gray"}`}>{item.status}</span>
            </td>
            <td>
              <div className="progress-bar">
                <div style={{ width: `${Math.round(item.progress * 100)}%` }} />
                <span>{Math.round(item.progress * 100)}%</span>
              </div>
            </td>
            <td>
              <button
                className="btn icon-btn"
                title="Remove"
                onClick={() => remove.mutate(item.id)}
              >
                ✕
              </button>
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function History() {
  const { data, isLoading } = useQuery({
    queryKey: ["history"],
    queryFn: () => api.get<HistoryItem[]>("/history"),
    refetchInterval: 10000,
  });

  if (isLoading) return <Spinner />;
  if (!data || data.length === 0) return <EmptyState icon="🕘" title="No history yet" />;

  return (
    <table className="data-table">
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
            <td>
              <span className={`pill ${statusPill[ev.event] ?? "gray"}`}>{ev.event}</span>
            </td>
            <td>{ev.series_title}</td>
            <td style={{ color: "var(--text-dim)", wordBreak: "break-all" }}>{ev.detail}</td>
            <td>{ev.source_name}</td>
            <td style={{ color: "var(--text-dim)" }}>
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
