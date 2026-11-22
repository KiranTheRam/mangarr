import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../api/client";
import type { IssueScanResult, LibraryIssue } from "../api/types";
import { EmptyState, ErrorNotice, Spinner, Toolbar } from "../components/common";
import { IssueCard } from "../components/MetadataIssues";
import { Icon } from "../components/Icon";

export default function Issues() {
  const queryClient = useQueryClient();
  const [tab, setTab] = useState<"active" | "ignored">("active");
  const ignored = tab === "ignored";
  const { data, isLoading, error, refetch } = useQuery({
    queryKey: ["issues", ignored],
    queryFn: () => api.get<LibraryIssue[]>(`/issues?ignored=${ignored}`),
    refetchInterval: 30000,
  });
  const invalidate = () => {
    queryClient.invalidateQueries({ queryKey: ["issues"] });
    queryClient.invalidateQueries({ queryKey: ["series-issues"] });
  };
  const scan = useMutation({
    mutationFn: () => api.post<IssueScanResult>("/issues/scan"),
    onSuccess: invalidate,
  });
  return (
    <>
      <Toolbar title="Issues">
        <button className={`btn${tab === "active" ? " primary" : ""}`} onClick={() => setTab("active")}>
          Active
        </button>
        <button className={`btn${tab === "ignored" ? " primary" : ""}`} onClick={() => setTab("ignored")}>
          Ignored
        </button>
        <button className="btn" disabled={scan.isPending} onClick={() => scan.mutate()}>
          <Icon name="scan" /> {scan.isPending ? "Scanning…" : "Scan metadata"}
        </button>
      </Toolbar>
      <div className="content issues-page">
        <ErrorNotice error={error || scan.error} retry={() => void refetch()} />
        {scan.data && (
          <div className="scan-banner">
            <strong>Metadata scan complete.</strong> {scan.data.active} active, {scan.data.ignored} ignored.
          </div>
        )}
        {isLoading ? <Spinner /> : !data || data.length === 0 ? (
          <EmptyState
            icon={ignored ? "history" : "check"}
            title={ignored ? "No ignored issues" : "No active issues"}
            hint={ignored ? "Ignored metadata warnings remain here until fixed." : "Scan metadata to check files now."}
          />
        ) : (
          <div className="issue-list">
            {data.map((issue) => <IssueCard key={issue.id} issue={issue} onChanged={invalidate} />)}
          </div>
        )}
      </div>
    </>
  );
}
