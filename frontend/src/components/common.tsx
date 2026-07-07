import type { ReactNode } from "react";

export function Toolbar({ title, children }: { title?: string; children?: ReactNode }) {
  return (
    <div className="toolbar">
      {title && <h1>{title}</h1>}
      {children}
    </div>
  );
}

export function Spinner() {
  return (
    <div className="center">
      <div className="spinner" />
    </div>
  );
}

export function EmptyState({ icon, title, hint }: { icon: string; title: string; hint?: string }) {
  return (
    <div className="empty-state">
      <div className="big">{icon}</div>
      <h3>{title}</h3>
      {hint && <p style={{ marginTop: 8 }}>{hint}</p>}
    </div>
  );
}

export function Modal({
  title,
  onClose,
  children,
}: {
  title: string;
  onClose: () => void;
  children: ReactNode;
}) {
  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div className="modal" onClick={(e) => e.stopPropagation()}>
        <div className="modal-header">
          {title}
          <button onClick={onClose} style={{ fontSize: 18, color: "var(--text-dim)" }}>
            ✕
          </button>
        </div>
        <div className="modal-body">{children}</div>
      </div>
    </div>
  );
}

export function Toggle({ on, onChange }: { on: boolean; onChange: (v: boolean) => void }) {
  return <button type="button" className={`toggle${on ? " on" : ""}`} onClick={() => onChange(!on)} />;
}

export function formatBytes(bytes: number): string {
  if (!bytes) return "—";
  const units = ["B", "KiB", "MiB", "GiB", "TiB"];
  let i = 0;
  let v = bytes;
  while (v >= 1024 && i < units.length - 1) {
    v /= 1024;
    i++;
  }
  return `${v.toFixed(v >= 100 ? 0 : 1)} ${units[i]}`;
}

export function chapterLabel(number: number, volume?: number | null): string {
  const ch = Number.isInteger(number) ? number.toString() : number.toFixed(1);
  return volume != null ? `Vol. ${volume} Ch. ${ch}` : `Ch. ${ch}`;
}

export const statusPill: Record<string, string> = {
  releasing: "blue",
  finished: "green",
  hiatus: "orange",
  cancelled: "red",
  not_yet_released: "gray",
  unknown: "gray",
  queued: "gray",
  downloading: "blue",
  importing: "orange",
  done: "green",
  failed: "red",
  grabbed: "blue",
  imported: "green",
  deleted: "red",
};
