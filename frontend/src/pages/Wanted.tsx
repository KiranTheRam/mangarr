import { useEffect, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { api } from "../api/client";
import type { WantedItem } from "../api/types";
import { chapterLabel, EmptyState, ErrorNotice, Spinner, Toolbar } from "../components/common";

export default function Wanted() {
  const [offset, setOffset] = useState(0);
  const pageSize = 100;
  const { data: page, isLoading, error, refetch } = useQuery({
    queryKey: ["wanted", offset],
    queryFn: () => api.get<{ items: WantedItem[]; total: number }>(`/wanted/page?limit=${pageSize}&offset=${offset}`),
    refetchInterval: 15000,
  });

  const data = page?.items;
  useEffect(() => {
    if (page && offset > 0 && offset >= page.total) setOffset(Math.max(0, Math.ceil(page.total / pageSize) - 1) * pageSize);
  }, [page, offset]);

  return (
    <>
      <Toolbar title="Wanted">
        <button className="btn" disabled={offset === 0 || isLoading} onClick={() => setOffset(Math.max(0, offset - pageSize))}>Previous</button>
        {page && <span>{page.total === 0 ? "0" : `${offset + 1}–${offset + data!.length}`} of {page.total} chapters</span>}
        <button className="btn" disabled={!page || offset + pageSize >= page.total || isLoading} onClick={() => setOffset(offset + pageSize)}>Next</button>
      </Toolbar>
      <div className="content">
        <ErrorNotice error={error} retry={() => void refetch()} />
        {isLoading ? (
          <Spinner />
        ) : !data && error ? null : !data || data.length === 0 ? (
          <EmptyState
            icon="✔"
            title="Nothing wanted"
            hint="All monitored chapters are downloaded."
          />
        ) : (
          <table className="data-table card-table wanted-table">
            <thead>
              <tr>
                <th style={{ width: 280 }}>Series</th>
                <th style={{ width: 140 }}>Chapter</th>
                <th>Title</th>
              </tr>
            </thead>
            <tbody>
              {data.map((w) => (
                <tr key={w.chapter_id}>
                  <td className="cell-series">
                    <Link to={`/series/${w.series_id}`} style={{ color: "var(--info)" }}>
                      {w.series_title}
                    </Link>
                  </td>
                  <td className="cell-chapter">{chapterLabel(w.number, w.volume)}</td>
                  <td className="cell-wtitle" style={{ color: "var(--text-dim)" }}>{w.title || "—"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </>
  );
}
