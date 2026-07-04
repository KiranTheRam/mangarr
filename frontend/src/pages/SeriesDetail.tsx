import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useNavigate, useParams } from "react-router-dom";
import { api } from "../api/client";
import type { Release, SeriesDetail as SeriesDetailType } from "../api/types";
import {
  chapterLabel,
  formatBytes,
  Modal,
  Spinner,
  statusPill,
  Toolbar,
} from "../components/common";

function InteractiveSearch({
  seriesId,
  chapterId,
  title,
  onClose,
}: {
  seriesId: number;
  chapterId?: number;
  title: string;
  onClose: () => void;
}) {
  const queryClient = useQueryClient();
  const params = chapterId != null ? `chapter_id=${chapterId}` : `series_id=${seriesId}`;
  const { data, isLoading, isError, error } = useQuery({
    queryKey: ["releases", params],
    queryFn: () => api.get<Release[]>(`/search/releases?${params}`),
  });

  const grab = useMutation({
    mutationFn: (release: Release) =>
      api.post("/queue/grab", release.kind === "direct"
        ? { chapter_id: chapterId, source_name: release.source_name, external_id: release.external_id }
        : { series_id: seriesId, magnet: release.magnet, title: release.title }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["queue"] });
      onClose();
    },
  });

  return (
    <Modal title={`Search — ${title}`} onClose={onClose}>
      {isLoading ? (
        <Spinner />
      ) : isError ? (
        <div className="error-banner">{(error as Error).message}</div>
      ) : !data || data.length === 0 ? (
        <p style={{ color: "var(--text-dim)" }}>
          No releases found. Check source links and that sources are enabled in Settings.
        </p>
      ) : (
        <>
          {grab.isError && <div className="error-banner">{(grab.error as Error).message}</div>}
          <table className="data-table">
            <thead>
              <tr>
                <th>Source</th>
                <th>Title</th>
                <th>Size</th>
                <th>Peers</th>
                <th></th>
              </tr>
            </thead>
            <tbody>
              {data.map((r, i) => (
                <tr key={i}>
                  <td>
                    <span className={`pill ${r.kind === "torrent" ? "orange" : "blue"}`}>
                      {r.source_name}
                    </span>
                  </td>
                  <td>
                    {r.url ? (
                      <a href={r.url} target="_blank" rel="noreferrer" style={{ color: "var(--info)" }}>
                        {r.title}
                      </a>
                    ) : (
                      r.title
                    )}
                  </td>
                  <td>{r.kind === "torrent" ? formatBytes(r.size_bytes) : "—"}</td>
                  <td>{r.kind === "torrent" ? `${r.seeders}/${r.leechers}` : "—"}</td>
                  <td>
                    <button
                      className="btn icon-btn"
                      title="Grab"
                      disabled={grab.isPending}
                      onClick={() => grab.mutate(r)}
                    >
                      ⇓
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </>
      )}
    </Modal>
  );
}

export default function SeriesDetail() {
  const { id } = useParams();
  const seriesId = Number(id);
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const [search, setSearch] = useState<{ chapterId?: number; title: string } | null>(null);

  const { data: series, isLoading } = useQuery({
    queryKey: ["series", seriesId],
    queryFn: () => api.get<SeriesDetailType>(`/series/${seriesId}`),
    refetchInterval: 10000,
  });

  const invalidate = () => {
    queryClient.invalidateQueries({ queryKey: ["series", seriesId] });
    queryClient.invalidateQueries({ queryKey: ["series"] });
  };

  const toggleMonitor = useMutation({
    mutationFn: () => api.put(`/series/${seriesId}`, { monitored: !series?.monitored }),
    onSuccess: invalidate,
  });

  const refresh = useMutation({
    mutationFn: () => api.post(`/series/${seriesId}/refresh`),
    onSuccess: () => setTimeout(invalidate, 4000),
  });

  const deleteSeries = useMutation({
    mutationFn: () => api.del(`/series/${seriesId}`),
    onSuccess: () => {
      invalidate();
      navigate("/");
    },
  });

  const toggleChapter = useMutation({
    mutationFn: (args: { chapterIds: number[]; monitored: boolean }) =>
      api.put(`/series/${seriesId}/chapters/monitor`, {
        chapter_ids: args.chapterIds,
        monitored: args.monitored,
      }),
    onSuccess: invalidate,
  });

  if (isLoading || !series) {
    return (
      <>
        <Toolbar title="Series" />
        <Spinner />
      </>
    );
  }

  const chapters = [...series.chapters].sort((a, b) => b.number - a.number);

  return (
    <>
      <Toolbar title={series.title}>
        <button className="btn" onClick={() => refresh.mutate()} disabled={refresh.isPending}>
          ⟳ Refresh
        </button>
        <button
          className="btn"
          onClick={() => setSearch({ title: `${series.title} (all releases)` })}
        >
          🔍 Search Releases
        </button>
        <button className="btn" onClick={() => toggleMonitor.mutate()}>
          {series.monitored ? "🔖 Monitored" : "◻ Unmonitored"}
        </button>
        <button
          className="btn danger"
          onClick={() => {
            if (confirm(`Remove "${series.title}" from library? Files on disk are kept.`))
              deleteSeries.mutate();
          }}
        >
          ✕
        </button>
      </Toolbar>
      <div className="content">
        <div
          className="series-header"
          style={series.banner_url ? { backgroundImage: `url(${series.banner_url})` } : {}}
        >
          {series.cover_url && <img className="cover" src={series.cover_url} alt="" />}
          <div>
            <h2>
              {series.title}{" "}
              {series.year && <span style={{ color: "var(--text-dim)" }}>({series.year})</span>}
            </h2>
            <div className="series-meta">
              <span className={`pill ${statusPill[series.status] ?? "gray"}`}>{series.status}</span>
              <span>
                {series.downloaded_count} / {series.chapter_count} chapters
              </span>
              {series.total_volumes && <span>{series.total_volumes} volumes</span>}
            </div>
            <div style={{ marginBottom: 10 }}>
              {series.genres
                .split(",")
                .filter(Boolean)
                .map((g) => (
                  <span className="tag" key={g}>
                    {g}
                  </span>
                ))}
            </div>
            <div className="series-desc" dangerouslySetInnerHTML={{ __html: series.description }} />
            {series.source_links.length > 0 && (
              <div style={{ marginTop: 12 }}>
                {series.source_links.map((sl) => (
                  <a key={sl.id} href={sl.external_url} target="_blank" rel="noreferrer">
                    <span className="tag" title={sl.external_title}>
                      🔗 {sl.source_name}
                    </span>
                  </a>
                ))}
              </div>
            )}
          </div>
        </div>

        {chapters.length === 0 ? (
          <p style={{ color: "var(--text-dim)" }}>
            No chapters found yet — sources may still be syncing. Use Refresh to retry.
          </p>
        ) : (
          <table className="data-table">
            <thead>
              <tr>
                <th style={{ width: 36 }}></th>
                <th style={{ width: 130 }}>Chapter</th>
                <th>Title</th>
                <th style={{ width: 120 }}>Status</th>
                <th style={{ width: 90 }}></th>
              </tr>
            </thead>
            <tbody>
              {chapters.map((ch) => (
                <tr key={ch.id}>
                  <td>
                    <button
                      className={`monitor-toggle${ch.monitored ? " on" : ""}`}
                      title={ch.monitored ? "Monitored" : "Unmonitored"}
                      onClick={() =>
                        toggleChapter.mutate({ chapterIds: [ch.id], monitored: !ch.monitored })
                      }
                    >
                      {ch.monitored ? "🔖" : "◻"}
                    </button>
                  </td>
                  <td>{chapterLabel(ch.number, ch.volume)}</td>
                  <td style={{ color: ch.title ? "inherit" : "var(--text-faint)" }}>
                    {ch.title || "—"}
                  </td>
                  <td>
                    {ch.downloaded ? (
                      <span className="pill green" title={ch.file_path}>
                        Downloaded
                      </span>
                    ) : (
                      <span className="pill gray">Missing</span>
                    )}
                  </td>
                  <td>
                    <button
                      className="btn icon-btn"
                      title="Interactive search"
                      onClick={() =>
                        setSearch({
                          chapterId: ch.id,
                          title: `${series.title} ${chapterLabel(ch.number, ch.volume)}`,
                        })
                      }
                    >
                      🔍
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>

      {search && (
        <InteractiveSearch
          seriesId={seriesId}
          chapterId={search.chapterId}
          title={search.title}
          onClose={() => setSearch(null)}
        />
      )}
    </>
  );
}
