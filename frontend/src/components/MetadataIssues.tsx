import { useState } from "react";
import { useMutation } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { api } from "../api/client";
import type { LibraryIssue } from "../api/types";
import { chapterLabel, ErrorNotice, Modal } from "./common";
import { Icon } from "./Icon";

function valueLabel(value: string | null, field: string) {
  if (value == null || value === "") return field === "volume" ? "Remove Volume tag" : "Remove field";
  return value;
}

function IssueReview({ issue, onClose, onChanged }: {
  issue: LibraryIssue;
  onClose: () => void;
  onChanged: () => void;
}) {
  const resolve = useMutation({
    mutationFn: () => api.post(`/issues/${issue.id}/resolve`),
    onSuccess: () => { onChanged(); onClose(); },
  });
  return (
    <Modal title="Review recommended metadata change" onClose={onClose}>
      <p className="issue-review-title">{issue.title}</p>
      <p className="issue-detail">{issue.detail}</p>
      <div className="issue-change-grid">
        <div>
          <span>Current {issue.field.replace("_", " ")}</span>
          <strong>{issue.current_value ?? "Not set"}</strong>
        </div>
        <Icon name="arrow-right" />
        <div className="proposed">
          <span>Recommended</span>
          <strong>{valueLabel(issue.suggested_value, issue.field)}</strong>
        </div>
      </div>
      <p className="form-hint">
        Mangarr will update only this ComicInfo.xml field, preserve every page and other
        metadata field, then ask Kavita to rescan when that integration is enabled.
      </p>
      <ErrorNotice error={resolve.error} />
      <div className="modal-actions">
        <button className="btn" onClick={onClose}>Cancel</button>
        <button className="btn primary" disabled={resolve.isPending} onClick={() => resolve.mutate()}>
          {resolve.isPending ? "Applying…" : "Accept recommended change"}
        </button>
      </div>
    </Modal>
  );
}

export function IssueCard({ issue, onChanged, compact = false }: {
  issue: LibraryIssue;
  onChanged: () => void;
  compact?: boolean;
}) {
  const [reviewing, setReviewing] = useState(false);
  const state = useMutation({
    mutationFn: () => api.post(`/issues/${issue.id}/${issue.ignored ? "restore" : "ignore"}`),
    onSuccess: onChanged,
  });
  const fileName = issue.file_path.split(/[\\/]/).pop() ?? issue.file_path;
  return (
    <div className={`issue-card${compact ? " compact" : ""}`}>
      <div className="issue-icon"><Icon name="warning" /></div>
      <div className="issue-body">
        <div className="issue-heading">
          <strong>{issue.title}</strong>
          <span className="pill orange">ComicInfo.xml</span>
        </div>
        <div className="issue-context">
          <Link to={`/series/${issue.series_id}`}>{issue.series_title}</Link>
          {issue.chapter_number != null && <span>{chapterLabel(issue.chapter_number)}</span>}
          <span title={issue.file_path}>{fileName}</span>
        </div>
        <p className="issue-detail">{issue.detail}</p>
        <div className="issue-proposal">
          <span>{issue.field.replace("_", " ")}</span>
          <code>{issue.current_value ?? "not set"}</code>
          <Icon name="arrow-right" size={16} />
          <code className="recommended">{valueLabel(issue.suggested_value, issue.field)}</code>
        </div>
      </div>
      <div className="issue-actions">
        {!issue.ignored && (
          <button className="btn primary sm" onClick={() => setReviewing(true)}>
            Review fix
          </button>
        )}
        <button className="btn sm" disabled={state.isPending} onClick={() => state.mutate()}>
          {issue.ignored ? "Move to active" : "Ignore"}
        </button>
      </div>
      {state.error && <ErrorNotice error={state.error} />}
      {reviewing && (
        <IssueReview issue={issue} onClose={() => setReviewing(false)} onChanged={onChanged} />
      )}
    </div>
  );
}

export function SeriesIssues({ issues, onChanged }: {
  issues: LibraryIssue[];
  onChanged: () => void;
}) {
  if (issues.length === 0) return null;
  return (
    <section className="series-issues" aria-labelledby="series-issues-title">
      <div className="series-issues-header">
        <div>
          <h3 id="series-issues-title">Issues</h3>
          <p>{issues.length} metadata {issues.length === 1 ? "issue needs" : "issues need"} attention</p>
        </div>
        <Link className="btn sm" to="/issues">View all issues</Link>
      </div>
      {issues.map((issue) => (
        <IssueCard key={issue.id} issue={issue} onChanged={onChanged} compact />
      ))}
    </section>
  );
}
