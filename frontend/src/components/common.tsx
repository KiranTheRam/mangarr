import { useEffect, useId, useRef, type ReactNode } from "react";

export function Toolbar({
  title,
  className = "",
  children,
}: {
  title?: string;
  className?: string;
  children?: ReactNode;
}) {
  return (
    <div className={`toolbar${className ? ` ${className}` : ""}`}>
      {title && <h1>{title}</h1>}
      {children}
    </div>
  );
}

export function Spinner() {
  return (
    <div className="center">
      <div className="spinner" role="status" aria-label="Loading" />
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
  const dialogRef = useRef<HTMLDialogElement>(null);
  const titleId = useId();
  useEffect(() => {
    const dialog = dialogRef.current!;
    const previousFocus = document.activeElement;
    dialog.showModal();
    return () => {
      dialog.close();
      if (previousFocus instanceof HTMLElement && previousFocus.isConnected) previousFocus.focus();
    };
  }, []);
  return (
    <dialog ref={dialogRef} className="modal-backdrop" aria-labelledby={titleId}
      onCancel={(event) => { event.preventDefault(); event.stopPropagation(); onClose(); }}
      onClick={(event) => { if (event.target === event.currentTarget) onClose(); }}>
      <div className="modal" onClick={(e) => e.stopPropagation()}>
        <div className="modal-header">
          <span id={titleId}>{title}</span>
          <button type="button" aria-label="Close dialog" onClick={onClose} style={{ fontSize: 18, color: "var(--text-dim)" }}>
            ✕
          </button>
        </div>
        <div className="modal-body">{children}</div>
      </div>
    </dialog>
  );
}

export function ErrorNotice({ error, retry }: { error: unknown; retry?: () => void }) {
  if (!error) return null;
  return <div className="error-banner" role="alert">
    {error instanceof Error ? error.message : "Request failed. Please try again."}
    {retry && <button className="btn" style={{ marginLeft: 12 }} onClick={retry}>Retry</button>}
  </div>;
}

export function Toggle({
  on,
  onChange,
  label,
  disabled = false,
}: {
  on: boolean;
  onChange: (v: boolean) => void;
  label?: string;
  disabled?: boolean;
}) {
  return (
    <button
      type="button"
      className={`toggle${on ? " on" : ""}`}
      aria-label={label}
      aria-pressed={on}
      disabled={disabled}
      onClick={() => onChange(!on)}
    />
  );
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
  upgraded: "green",
  merged: "green",
  unmonitored: "gray",
  deleted: "red",
};
