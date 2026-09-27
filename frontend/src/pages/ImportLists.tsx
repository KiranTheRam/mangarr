import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { api } from "../api/client";
import type {
  ImportList,
  ImportListConfig,
  ImportListEntry,
  ImportListKind,
  ImportListPreviewItem,
  ImportListProvider,
  ImportListSyncResult,
  MonitorMode,
  RootFolder,
  Settings,
} from "../api/types";
import { MONITOR_MODES } from "../components/SeriesAutomation";
import { EmptyState, ErrorNotice, Modal, Spinner, Toggle, Toolbar } from "../components/common";
import { AddTabs } from "./AddSeries";

const PROVIDER_HINTS: Record<ImportListKind, string> = {
  anilist: "Reads a public AniList profile's manga list — no login needed.",
  myanimelist:
    "Needs a MyAnimeList API client ID (create one at myanimelist.net/apiconfig). Titles are matched through AniList.",
  mangadex: "Reads the library of the MangaDex account set in Settings → MangaDex Account.",
  mangaupdates: "MangaUpdates lists are private, so this logs in with the account's username and password.",
};

const ENTRY_STATUS_PILL: Record<ImportListEntry["status"], string> = {
  added: "green",
  existing: "blue",
  failed: "red",
  skipped: "gray",
};

function relativeTime(iso: string | null): string {
  if (!iso) return "never";
  const date = new Date(iso.endsWith("Z") || iso.includes("+") ? iso : `${iso}Z`);
  const minutes = Math.round((Date.now() - date.getTime()) / 60000);
  if (minutes < 1) return "just now";
  if (minutes < 60) return `${minutes} min ago`;
  const hours = Math.round(minutes / 60);
  if (hours < 48) return `${hours} h ago`;
  return date.toLocaleDateString();
}

function emptyConfig(provider: ImportListProvider, rootFolderId: number): ImportListConfig {
  return {
    name: `${provider.label} list`,
    kind: provider.kind,
    enabled: true,
    username: "",
    password: "",
    client_id: "",
    statuses: [...provider.default_statuses],
    root_folder_id: rootFolderId,
    monitored: true,
    monitor_mode: "all",
    search_now: false,
  };
}

function PreviewList({
  items,
  listId,
  onSkipped,
}: {
  items: ImportListPreviewItem[];
  listId: number | null;
  onSkipped: () => void;
}) {
  const [skipped, setSkipped] = useState<Set<string>>(new Set());
  const skip = useMutation({
    mutationFn: (item: ImportListPreviewItem) =>
      api.post(`/importlists/${listId}/entries/skip`, { key: item.key, title: item.title }),
    onSuccess: (_, item) => {
      setSkipped((prev) => new Set(prev).add(item.key));
      onSkipped();
    },
  });
  const toAdd = items.filter((i) => i.action === "add" && !skipped.has(i.key));
  const inLibrary = items.filter((i) => i.action === "in_library").length;
  const seen = items.filter((i) => i.action === "seen").length;
  const shown = [...toAdd, ...items.filter((i) => i.action !== "add")].slice(0, 200);
  return (
    <div className="list-preview">
      <p className="form-hint">
        {items.length} entr{items.length === 1 ? "y" : "ies"} on the list: <strong>{toAdd.length} would be added</strong>
        {inLibrary > 0 && `, ${inLibrary} already in the library`}
        {seen > 0 && `, ${seen} handled by an earlier sync`}.
      </p>
      <ErrorNotice error={skip.error} />
      <div className="list-preview-rows">
        {shown.map((item) => (
          <div className="list-preview-row" key={item.key}>
            {item.cover_url ? <img src={item.cover_url} alt="" /> : <div className="import-cover-empty" />}
            <span className="list-preview-title">
              {item.series_id ? <Link to={`/series/${item.series_id}`}>{item.title}</Link> : item.title}
              {item.year ? <span className="dim"> ({item.year})</span> : null}
            </span>
            {item.action === "add" ? (
              skipped.has(item.key) ? (
                <span className="pill gray">Skipped</span>
              ) : (
                <>
                  <span className="pill blue">Add</span>
                  {listId !== null && (
                    <button
                      className="btn sm"
                      type="button"
                      title="Never add this entry"
                      disabled={skip.isPending}
                      onClick={() => skip.mutate(item)}
                    >
                      Skip
                    </button>
                  )}
                </>
              )
            ) : item.action === "in_library" ? (
              <span className="pill green">In library</span>
            ) : (
              <span className="pill gray">Handled</span>
            )}
          </div>
        ))}
        {items.length > shown.length && (
          <p className="form-hint">…and {items.length - shown.length} more.</p>
        )}
      </div>
    </div>
  );
}

function ListEditor({
  list,
  providers,
  rootFolders,
  onClose,
}: {
  list: ImportList | null;
  providers: ImportListProvider[];
  rootFolders: RootFolder[];
  onClose: () => void;
}) {
  const queryClient = useQueryClient();
  const [form, setForm] = useState<ImportListConfig>(
    () => list ?? emptyConfig(providers[0], rootFolders[0].id),
  );
  const [preview, setPreview] = useState<ImportListPreviewItem[] | null>(null);
  const provider = providers.find((p) => p.kind === form.kind)!;
  const set = <K extends keyof ImportListConfig>(key: K, value: ImportListConfig[K]) => {
    setForm((prev) => ({ ...prev, [key]: value }));
    setPreview(null);
  };

  const previewMutation = useMutation({
    mutationFn: () =>
      api.post<ImportListPreviewItem[]>(`/importlists/preview${list ? `?list_id=${list.id}` : ""}`, form),
    onSuccess: setPreview,
  });
  const save = useMutation({
    mutationFn: () =>
      list ? api.put<ImportList>(`/importlists/${list.id}`, form) : api.post<ImportList>("/importlists", form),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["importlists"] });
      onClose();
    },
  });

  const missing =
    !form.name.trim() ||
    (provider.needs_username && !form.username.trim()) ||
    (provider.needs_password && !form.password) ||
    (provider.needs_client_id && !form.client_id.trim()) ||
    form.statuses.length === 0;

  return (
    <Modal title={list ? `Edit — ${list.name}` : "Add import list"} onClose={onClose}>
      {!list && (
        <div className="form-row">
          <label htmlFor="list-kind">Site</label>
          <select
            id="list-kind"
            value={form.kind}
            onChange={(e) => {
              const next = providers.find((p) => p.kind === e.target.value)!;
              setForm({ ...emptyConfig(next, form.root_folder_id), monitored: form.monitored,
                monitor_mode: form.monitor_mode, search_now: form.search_now });
              setPreview(null);
            }}
          >
            {providers.map((p) => (
              <option key={p.kind} value={p.kind}>
                {p.label}
              </option>
            ))}
          </select>
        </div>
      )}
      <p className="form-hint">{PROVIDER_HINTS[form.kind]}</p>
      <div className="form-row">
        <label htmlFor="list-name">Name</label>
        <input id="list-name" type="text" value={form.name} onChange={(e) => set("name", e.target.value)} />
      </div>
      {provider.needs_username && (
        <div className="form-row">
          <label htmlFor="list-user">Username</label>
          <input
            id="list-user"
            type="text"
            autoCapitalize="none"
            autoCorrect="off"
            value={form.username}
            onChange={(e) => set("username", e.target.value)}
          />
        </div>
      )}
      {provider.needs_password && (
        <div className="form-row">
          <label htmlFor="list-password">Password</label>
          <input
            id="list-password"
            type="password"
            value={form.password}
            onChange={(e) => set("password", e.target.value)}
          />
        </div>
      )}
      {provider.needs_client_id && (
        <div className="form-row">
          <label htmlFor="list-client">Client ID</label>
          <input
            id="list-client"
            type="text"
            autoCapitalize="none"
            autoCorrect="off"
            value={form.client_id}
            onChange={(e) => set("client_id", e.target.value)}
          />
        </div>
      )}
      <div className="form-row">
        <label>Lists to follow</label>
        <div className="list-statuses">
          {Object.entries(provider.statuses).map(([value, label]) => (
            <label key={value} className="list-status-option">
              <input
                type="checkbox"
                checked={form.statuses.includes(value)}
                onChange={(e) =>
                  set(
                    "statuses",
                    e.target.checked ? [...form.statuses, value] : form.statuses.filter((s) => s !== value),
                  )
                }
              />
              {label}
            </label>
          ))}
        </div>
      </div>
      {rootFolders.length > 1 && (
        <div className="form-row">
          <label htmlFor="list-root">Root folder</label>
          <select
            id="list-root"
            value={form.root_folder_id}
            onChange={(e) => set("root_folder_id", Number(e.target.value))}
          >
            {rootFolders.map((rf) => (
              <option key={rf.id} value={rf.id}>
                {rf.path}
              </option>
            ))}
          </select>
        </div>
      )}
      <div className="form-row">
        <label>Monitor new series</label>
        <Toggle label="Monitor new series" on={form.monitored} onChange={(v) => set("monitored", v)} />
      </div>
      {form.monitored && (
        <div className="form-row">
          <label htmlFor="list-mode">Chapters to monitor</label>
          <select
            id="list-mode"
            value={form.monitor_mode}
            onChange={(e) => set("monitor_mode", e.target.value as MonitorMode)}
          >
            {/* a chapter threshold only makes sense for one series at a time */}
            {MONITOR_MODES.filter((m) => m.value !== "from_chapter").map((m) => (
              <option key={m.value} value={m.value}>
                {m.label}
              </option>
            ))}
          </select>
        </div>
      )}
      <div className="form-row">
        <label>Search for missing content</label>
        <Toggle label="Search for missing content" on={form.search_now} onChange={(v) => set("search_now", v)} />
      </div>
      <div className="form-row">
        <label>Sync automatically</label>
        <Toggle label="Sync automatically" on={form.enabled} onChange={(v) => set("enabled", v)} />
      </div>

      <ErrorNotice error={previewMutation.error} />
      <ErrorNotice error={save.error} />
      {previewMutation.isPending && <Spinner />}
      {preview && (
        <PreviewList
          items={preview}
          listId={list?.id ?? null}
          onSkipped={() => queryClient.invalidateQueries({ queryKey: ["importlists"] })}
        />
      )}

      <div className="modal-actions">
        <button
          className="btn"
          type="button"
          disabled={missing || previewMutation.isPending}
          onClick={() => previewMutation.mutate()}
        >
          Preview
        </button>
        <button className="btn primary" type="button" disabled={missing || save.isPending} onClick={() => save.mutate()}>
          {save.isPending ? "Saving…" : "Save"}
        </button>
      </div>
    </Modal>
  );
}

function EntriesModal({ list, onClose }: { list: ImportList; onClose: () => void }) {
  const queryClient = useQueryClient();
  const { data, isLoading, error } = useQuery({
    queryKey: ["importlist-entries", list.id],
    queryFn: () => api.get<ImportListEntry[]>(`/importlists/${list.id}/entries`),
  });
  const forget = useMutation({
    mutationFn: (entry: ImportListEntry) => api.del(`/importlists/${list.id}/entries/${entry.id}`),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["importlist-entries", list.id] });
      queryClient.invalidateQueries({ queryKey: ["importlists"] });
    },
  });
  return (
    <Modal title={`Entries — ${list.name}`} onClose={onClose}>
      <p className="form-hint">
        Every entry the list has produced. Each is handled once, so removing a series from the library
        doesn&apos;t bring it back; <em>Forget</em> an entry to have the next sync consider it again. Failed
        entries are retried automatically.
      </p>
      <ErrorNotice error={error} />
      <ErrorNotice error={forget.error} />
      {isLoading ? (
        <Spinner />
      ) : !data || data.length === 0 ? (
        <p className="form-hint">No entries yet — sync the list first.</p>
      ) : (
        <div className="list-preview-rows">
          {data.map((entry) => (
            <div className="list-preview-row" key={entry.id}>
              <span className={`pill ${ENTRY_STATUS_PILL[entry.status] ?? "gray"}`}>{entry.status}</span>
              <span className="list-preview-title">
                {entry.series_id && entry.status !== "skipped" ? (
                  <Link to={`/series/${entry.series_id}`}>{entry.title}</Link>
                ) : (
                  entry.title
                )}
                {entry.detail && <span className="dim list-entry-detail">{entry.detail}</span>}
              </span>
              <button
                className="btn sm"
                type="button"
                disabled={forget.isPending}
                onClick={() => forget.mutate(entry)}
              >
                Forget
              </button>
            </div>
          ))}
        </div>
      )}
    </Modal>
  );
}

function SyncInterval() {
  const queryClient = useQueryClient();
  const { data: settings } = useQuery({
    queryKey: ["settings"],
    queryFn: () => api.get<Settings>("/settings"),
  });
  const [hours, setHours] = useState("");
  useEffect(() => {
    if (settings) setHours(settings.import_list_sync_hours ?? "6");
  }, [settings]);
  const save = useMutation({
    mutationFn: () => api.put<Settings>("/settings", { import_list_sync_hours: hours.trim() }),
    onSuccess: (data) => queryClient.setQueryData(["settings"], data),
  });
  const dirty = !!settings && hours.trim() !== (settings.import_list_sync_hours ?? "6");
  return (
    <div className="list-interval">
      <label htmlFor="list-interval">Sync enabled lists every</label>
      <input
        id="list-interval"
        type="text"
        inputMode="numeric"
        value={hours}
        onChange={(e) => setHours(e.target.value)}
      />
      <span>hours</span>
      {dirty && (
        <button className="btn sm primary" type="button" disabled={save.isPending} onClick={() => save.mutate()}>
          Save
        </button>
      )}
      {save.isError && <span className="danger-text">{(save.error as Error).message}</span>}
    </div>
  );
}

function ListCard({
  list,
  providers,
  onEdit,
  onEntries,
}: {
  list: ImportList;
  providers: ImportListProvider[];
  onEdit: () => void;
  onEntries: () => void;
}) {
  const queryClient = useQueryClient();
  const provider = providers.find((p) => p.kind === list.kind);
  const [result, setResult] = useState<ImportListSyncResult | null>(null);
  const [confirmDelete, setConfirmDelete] = useState(false);
  const sync = useMutation({
    mutationFn: () => api.post<ImportListSyncResult>(`/importlists/${list.id}/sync`),
    onSuccess: (data) => {
      setResult(data);
      queryClient.invalidateQueries({ queryKey: ["series"] });
    },
    onSettled: () => queryClient.invalidateQueries({ queryKey: ["importlists"] }),
  });
  const remove = useMutation({
    mutationFn: () => api.del(`/importlists/${list.id}`),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["importlists"] }),
  });
  const counts = list.entry_counts;
  const statusLabels = list.statuses.map((s) => provider?.statuses[s] ?? s).join(", ");
  return (
    <div className="list-card">
      <div className="list-card-head">
        <div>
          <h3>{list.name}</h3>
          <div className="dim">
            {provider?.label ?? list.kind}
            {list.username && ` · ${list.username}`} · {statusLabels}
          </div>
        </div>
        {!list.enabled && <span className="pill gray">Manual only</span>}
      </div>
      <div className="list-card-stats">
        <span>Synced {relativeTime(list.last_synced_at)}</span>
        {(counts.added ?? 0) > 0 && <span className="pill green">{counts.added} added</span>}
        {(counts.existing ?? 0) > 0 && <span className="pill blue">{counts.existing} already owned</span>}
        {(counts.failed ?? 0) > 0 && <span className="pill red">{counts.failed} failed</span>}
        {(counts.skipped ?? 0) > 0 && <span className="pill gray">{counts.skipped} skipped</span>}
      </div>
      {list.last_error && <div className="danger-text">Last sync failed: {list.last_error}</div>}
      {result && (
        <div className="list-sync-result" role="status">
          Found {result.fetched} · added {result.added}
          {result.existing > 0 && ` · ${result.existing} already owned`}
          {result.failed > 0 && ` · ${result.failed} failed`}
        </div>
      )}
      <ErrorNotice error={sync.error} />
      <ErrorNotice error={remove.error} />
      <div className="list-card-actions">
        <button className="btn primary sm" type="button" disabled={sync.isPending} onClick={() => sync.mutate()}>
          {sync.isPending ? "Syncing…" : "Sync now"}
        </button>
        <button className="btn sm" type="button" onClick={onEdit}>
          Edit
        </button>
        <button className="btn sm" type="button" onClick={onEntries}>
          Entries
        </button>
        {confirmDelete ? (
          <>
            <button className="btn sm danger" type="button" disabled={remove.isPending} onClick={() => remove.mutate()}>
              Delete list
            </button>
            <button className="btn sm" type="button" onClick={() => setConfirmDelete(false)}>
              Cancel
            </button>
          </>
        ) : (
          <button className="btn sm" type="button" title="Series it added stay in the library" onClick={() => setConfirmDelete(true)}>
            Delete…
          </button>
        )}
      </div>
    </div>
  );
}

export default function ImportLists() {
  const { data: lists, isLoading, error, refetch } = useQuery({
    queryKey: ["importlists"],
    queryFn: () => api.get<ImportList[]>("/importlists"),
  });
  const { data: providers } = useQuery({
    queryKey: ["importlist-providers"],
    queryFn: () => api.get<ImportListProvider[]>("/importlists/providers"),
    staleTime: Infinity,
  });
  const { data: rootFolders } = useQuery({
    queryKey: ["rootfolders"],
    queryFn: () => api.get<RootFolder[]>("/rootfolders"),
  });
  const [editing, setEditing] = useState<ImportList | "new" | null>(null);
  const [entriesOf, setEntriesOf] = useState<ImportList | null>(null);
  const ready = !!providers && !!rootFolders && rootFolders.length > 0;

  return (
    <>
      <Toolbar title="Add New Series" />
      <div className="content">
        <AddTabs />
        <p className="section-hint import-intro">
          Follow reading lists on other sites: new entries are added to the library on every sync, with
          the root folder and monitoring chosen per list.
        </p>
        <div className="import-controls">
          <button className="btn primary" disabled={!ready} onClick={() => setEditing("new")}>
            + Add list
          </button>
          <SyncInterval />
        </div>
        {rootFolders && rootFolders.length === 0 && (
          <div className="error-banner">No root folder configured — add one in Settings first.</div>
        )}
        <ErrorNotice error={error} retry={() => void refetch()} />
        {isLoading ? (
          <Spinner />
        ) : lists && lists.length === 0 ? (
          <EmptyState
            icon="☰"
            title="No import lists"
            hint="Add an AniList, MyAnimeList, MangaDex or MangaUpdates list to add what you read automatically."
          />
        ) : (
          providers &&
          lists?.map((list) => (
            <ListCard
              key={list.id}
              list={list}
              providers={providers}
              onEdit={() => setEditing(list)}
              onEntries={() => setEntriesOf(list)}
            />
          ))
        )}
      </div>
      {editing && ready && (
        <ListEditor
          list={editing === "new" ? null : editing}
          providers={providers}
          rootFolders={rootFolders}
          onClose={() => setEditing(null)}
        />
      )}
      {entriesOf && <EntriesModal list={entriesOf} onClose={() => setEntriesOf(null)} />}
    </>
  );
}
