import { useQuery } from "@tanstack/react-query";
import { Link, useNavigate } from "react-router-dom";
import { api } from "../api/client";
import type { Series } from "../api/types";
import { EmptyState, Spinner, Toolbar } from "../components/common";

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
  const { data, isLoading } = useQuery({
    queryKey: ["series"],
    queryFn: () => api.get<Series[]>("/series"),
  });

  return (
    <>
      <Toolbar title="Library">
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
        ) : (
          <div className="poster-grid">
            {data.map((s) => (
              <PosterCard key={s.id} series={s} />
            ))}
          </div>
        )}
      </div>
    </>
  );
}
