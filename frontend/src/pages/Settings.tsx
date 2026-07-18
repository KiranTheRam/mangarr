import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../api/client";
import type { ApiKey, RootFolder, Settings as SettingsType } from "../api/types";
import { FolderBrowser } from "../components/FolderBrowser";
import { Spinner, Toggle, Toolbar } from "../components/common";

function RootFolders() {
  const queryClient = useQueryClient();
  const [path, setPath] = useState("");
  const { data } = useQuery({
    queryKey: ["rootfolders"],
    queryFn: () => api.get<RootFolder[]>("/rootfolders"),
  });

  const add = useMutation({
    mutationFn: () => api.post("/rootfolders", { path }),
    onSuccess: () => {
      setPath("");
      queryClient.invalidateQueries({ queryKey: ["rootfolders"] });
    },
  });

  const remove = useMutation({
    mutationFn: (id: number) => api.del(`/rootfolders/${id}`),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["rootfolders"] }),
  });

  return (
    <div className="settings-section">
      <h3>Root Folders</h3>
      <p className="section-hint">Library locations where series folders and CBZ files are created.</p>
      {data?.map((rf) => (
        <div className="form-row" key={rf.id}>
          <label style={{ width: "auto", flex: 1 }}>{rf.path}</label>
          <button className="btn icon-btn" onClick={() => remove.mutate(rf.id)}>
            ✕
          </button>
        </div>
      ))}
      {add.isError && <div className="error-banner">{(add.error as Error).message}</div>}
      <div className="form-row">
        <input
          type="text"
          placeholder="/data/manga"
          value={path}
          onChange={(e) => setPath(e.target.value)}
          style={{ flex: 1, maxWidth: 380 }}
        />
        <button className="btn primary" disabled={!path || add.isPending} onClick={() => add.mutate()}>
          + Add
        </button>
      </div>
    </div>
  );
}

function ApiKeyRow({ apiKey, onRemove }: { apiKey: ApiKey; onRemove: () => void }) {
  const [copied, setCopied] = useState(false);
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(apiKey.key);
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    } catch {
      /* clipboard unavailable */
    }
  };
  const used = apiKey.last_used_at
    ? `last used ${new Date(apiKey.last_used_at).toLocaleString()}`
    : "never used";
  return (
    <div className="form-row" style={{ alignItems: "center" }}>
      <div style={{ flex: 1, minWidth: 0 }}>
        <div style={{ fontWeight: 600 }}>{apiKey.name}</div>
        <code
          style={{
            fontFamily: "ui-monospace, SFMono-Regular, Menlo, monospace",
            fontSize: 13,
            color: "var(--text-dim)",
            wordBreak: "break-all",
          }}
        >
          {apiKey.key}
        </code>
        <div style={{ fontSize: 12, color: "var(--text-faint)" }}>
          Added {new Date(apiKey.created_at).toLocaleDateString()} · {used}
        </div>
      </div>
      <button className="btn" onClick={copy}>
        {copied ? "Copied" : "Copy"}
      </button>
      <button className="btn icon-btn" title="Revoke key" aria-label={`Revoke ${apiKey.name}`} onClick={onRemove}>
        ✕
      </button>
    </div>
  );
}

function ApiKeys() {
  const queryClient = useQueryClient();
  const [name, setName] = useState("");
  const { data } = useQuery({
    queryKey: ["apikeys"],
    queryFn: () => api.get<ApiKey[]>("/apikeys"),
  });

  const add = useMutation({
    mutationFn: () => api.post<ApiKey>("/apikeys", { name: name.trim() }),
    onSuccess: () => {
      setName("");
      queryClient.invalidateQueries({ queryKey: ["apikeys"] });
    },
  });

  const remove = useMutation({
    mutationFn: (id: number) => api.del(`/apikeys/${id}`),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["apikeys"] }),
  });

  return (
    <div className="settings-section">
      <h3>API Keys</h3>
      <p className="section-hint">
        Create keys for external clients (e.g. NextPanel or scripts) to access the API. Send a
        key as the <code>X-Api-Key</code> header. Any key here grants full access — revoke ones
        you no longer use.
      </p>
      {data?.map((k) => (
        <ApiKeyRow key={k.id} apiKey={k} onRemove={() => remove.mutate(k.id)} />
      ))}
      {data && data.length === 0 && (
        <p style={{ color: "var(--text-faint)", fontSize: 13 }}>No API keys yet.</p>
      )}
      {add.isError && <div className="error-banner">{(add.error as Error).message}</div>}
      {remove.isError && <div className="error-banner">{(remove.error as Error).message}</div>}
      <div className="form-row">
        <input
          type="text"
          placeholder="Key name (e.g. NextPanel)"
          value={name}
          onChange={(e) => setName(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && name.trim() && !add.isPending) add.mutate();
          }}
          style={{ flex: 1, maxWidth: 380 }}
        />
        <button className="btn primary" disabled={!name.trim() || add.isPending} onClick={() => add.mutate()}>
          + Generate Key
        </button>
      </div>
    </div>
  );
}

const SOURCE_LABELS: Record<string, string> = {
  mangaplus: "MangaPlus",
  tcbscans: "TCB Scans",
  mangadex: "MangaDex",
  weebcentral: "WeebCentral",
  asura: "Asura Scans",
  viz: "VIZ (official metadata)",
  wikipedia: "Wikipedia (metadata)",
  nyaa: "Nyaa (torrents)",
};

const SOURCE_HINTS: Record<string, string> = {
  mangaplus: "Official same-day Shonen Jump. Needs a residential IP — bans datacenters.",
  viz: "Exact printed-volume mappings for VIZ-licensed series; does not download chapters.",
  wikipedia: "Chapter titles and printed-volume tables for publishers not covered by VIZ.",
  nyaa: "Sent to the download client below and imported when complete.",
};

/** Ordered, toggleable source list — stored as the comma-separated
 * source_priority setting, but never hand-edited as text. */
function SourcePriority({
  form,
  setForm,
}: {
  form: SettingsType;
  setForm: (f: SettingsType) => void;
}) {
  const known = Object.keys(form)
    .filter((k) => k.startsWith("source_") && k.endsWith("_enabled"))
    .map((k) => k.slice("source_".length, -"_enabled".length));
  const order = (form.source_priority ?? "")
    .split(",")
    .map((s) => s.trim())
    .filter((s, i, arr) => s && known.includes(s) && arr.indexOf(s) === i);
  for (const s of known) if (!order.includes(s)) order.push(s);

  const move = (index: number, delta: number) => {
    const next = [...order];
    const target = index + delta;
    if (target < 0 || target >= next.length) return;
    [next[index], next[target]] = [next[target], next[index]];
    setForm({ ...form, source_priority: next.join(",") });
  };

  return (
    <div className="priority-list">
      {order.map((name, i) => {
        const enabled = form[`source_${name}_enabled`] === "true";
        return (
          <div className={`priority-row${enabled ? "" : " disabled"}`} key={name}>
            <span className="priority-rank">{i + 1}</span>
            <span className="priority-arrows">
              <button className="btn icon-btn" disabled={i === 0} onClick={() => move(i, -1)} title="Higher priority">
                ↑
              </button>
              <button
                className="btn icon-btn"
                disabled={i === order.length - 1}
                onClick={() => move(i, 1)}
                title="Lower priority"
              >
                ↓
              </button>
            </span>
            <span className="priority-name">{SOURCE_LABELS[name] ?? name}</span>
            {SOURCE_HINTS[name] && <span className="priority-hint">{SOURCE_HINTS[name]}</span>}
            <Toggle
              on={enabled}
              onChange={(v) => setForm({ ...form, [`source_${name}_enabled`]: v ? "true" : "false" })}
            />
          </div>
        );
      })}
    </div>
  );
}

export default function Settings() {
  const queryClient = useQueryClient();
  const { data: saved, isLoading } = useQuery({
    queryKey: ["settings"],
    queryFn: () => api.get<SettingsType>("/settings"),
  });

  const [form, setForm] = useState<SettingsType>({});
  useEffect(() => {
    if (saved) setForm(saved);
  }, [saved]);

  const save = useMutation({
    mutationFn: () => api.put<SettingsType>("/settings", form),
    onSuccess: (data) => {
      queryClient.setQueryData(["settings"], data);
      setForm(data);
    },
  });

  const [qbtTest, setQbtTest] = useState<string | null>(null);
  const testQbt = useMutation({
    mutationFn: () =>
      api.post<{ version: string }>("/settings/qbittorrent/test", {
        url: form.qbittorrent_url,
        username: form.qbittorrent_username,
        password: form.qbittorrent_password,
      }),
    onSuccess: (d) => setQbtTest(`✔ Connected — qBittorrent ${d.version}`),
    onError: (e) => setQbtTest(`✖ ${(e as Error).message}`),
  });

  const [webhookTest, setWebhookTest] = useState<string | null>(null);
  const testWebhook = useMutation({
    mutationFn: () =>
      api.post("/settings/webhook/test", {
        url: form.webhook_url,
        secret: form.webhook_secret,
      }),
    onSuccess: () => setWebhookTest("✔ Webhook delivered"),
    onError: (e) => setWebhookTest(`✖ ${(e as Error).message}`),
  });

  const [browsing, setBrowsing] = useState(false);

  if (isLoading || !saved) {
    return (
      <>
        <Toolbar title="Settings" />
        <Spinner />
      </>
    );
  }

  const set = (key: string) => (e: React.ChangeEvent<HTMLInputElement | HTMLSelectElement>) =>
    setForm({ ...form, [key]: e.target.value });
  const setBool = (key: string) => (v: boolean) => setForm({ ...form, [key]: v ? "true" : "false" });

  const text = (key: string, secret = false) => (
    <input type={secret ? "password" : "text"} value={form[key] ?? ""} onChange={set(key)} />
  );

  return (
    <>
      <Toolbar title="Settings">
        {save.isSuccess && <span style={{ color: "var(--success)", fontSize: 13 }}>Saved</span>}
        <button className="btn primary" onClick={() => save.mutate()} disabled={save.isPending}>
          Save Changes
        </button>
      </Toolbar>
      <div className="content">
        <RootFolders />
        <ApiKeys />

        <div className="settings-section">
          <h3>Media Management</h3>
          <p className="section-hint">
            Naming uses {"{series}"}, {"{volume:02d}"} and {"{chapter:04.1f}"} placeholders. Output
            is Komga/Kavita-friendly CBZ.
          </p>
          <div className="form-row">
            <label>Chapter naming (with volume)</label>
            {text("naming_template")}
          </div>
          <div className="form-row">
            <label>Chapter naming (no volume)</label>
            {text("naming_template_no_volume")}
          </div>
          <div className="form-row">
            <label>Monitor interval (minutes)</label>
            {text("monitor_interval_minutes")}
          </div>
        </div>

        <div className="settings-section">
          <h3>Sources</h3>
          <p className="section-hint">
            Chapters are grabbed from the highest-priority enabled source that has them — use the
            arrows to reorder.
          </p>
          <SourcePriority form={form} setForm={setForm} />
        </div>

        <div className="settings-section">
          <h3>MangaDex Account</h3>
          <p className="section-hint">
            Optional but recommended: create a free MangaDex account and a personal API client
            (Settings → API Clients on mangadex.org). Without credentials, MangaDex limits guests
            to 10 chapters per day.
          </p>
          <div className="form-row">
            <label>Client ID</label>
            {text("mangadex_client_id")}
          </div>
          <div className="form-row">
            <label>Client secret</label>
            {text("mangadex_client_secret", true)}
          </div>
          <div className="form-row">
            <label>Username</label>
            {text("mangadex_username")}
          </div>
          <div className="form-row">
            <label>Password</label>
            {text("mangadex_password", true)}
          </div>
          <div className="form-row">
            <label>Language</label>
            {text("mangadex_language")}
          </div>
        </div>

        <div className="settings-section">
          <h3>Download Client — qBittorrent</h3>
          <p className="section-hint">
            Torrent grabs (Nyaa releases) are sent to qBittorrent and imported when complete.
          </p>
          <div className="form-row">
            <label>Enabled</label>
            <Toggle on={form.qbittorrent_enabled === "true"} onChange={setBool("qbittorrent_enabled")} />
          </div>
          <div className="form-row">
            <label>URL</label>
            {text("qbittorrent_url")}
          </div>
          <div className="form-row">
            <label>Username</label>
            {text("qbittorrent_username")}
          </div>
          <div className="form-row">
            <label>Password</label>
            {text("qbittorrent_password", true)}
          </div>
          <div className="form-row">
            <label>Category</label>
            {text("qbittorrent_category")}
          </div>
          <div className="form-row">
            <label>Downloads root</label>
            {text("downloads_dir")}
            <button className="btn" onClick={() => setBrowsing(true)}>
              Browse…
            </button>
          </div>
          <p className="section-hint">
            Base folder shared by mangarr and qBittorrent; the category is added automatically.
            Leave empty to use qBittorrent&apos;s default. Put it on the same filesystem/share as
            the library so imports can hardlink instead of copy.
          </p>
          <div className="form-row">
            <label>Import mode</label>
            <select value={form.import_mode ?? "hardlink"} onChange={set("import_mode")}>
              <option value="hardlink">Hardlink (keeps seeding, no extra space)</option>
              <option value="copy">Copy (safe across filesystems)</option>
            </select>
          </div>
          <div className="form-row">
            <label></label>
            <button className="btn" onClick={() => testQbt.mutate()} disabled={testQbt.isPending}>
              Test Connection
            </button>
            {qbtTest && (
              <span style={{ fontSize: 13, color: qbtTest.startsWith("✔") ? "var(--success)" : "var(--danger)" }}>
                {qbtTest}
              </span>
            )}
          </div>
        </div>

        <div className="settings-section">
          <h3>Connect — Webhook</h3>
          <p className="section-hint">
            Notify a request manager (e.g. NextPanel) whenever chapters are imported, so requests
            flip to Available instantly. Point the URL at NextPanel's
            /api/v1/webhooks/mangarr endpoint and paste the same webhook secret configured there.
          </p>
          <div className="form-row">
            <label>Enabled</label>
            <Toggle on={form.webhook_enabled === "true"} onChange={setBool("webhook_enabled")} />
          </div>
          <div className="form-row">
            <label>Webhook URL</label>
            {text("webhook_url")}
          </div>
          <div className="form-row">
            <label>Secret</label>
            {text("webhook_secret", true)}
          </div>
          <div className="form-row">
            <label></label>
            <button className="btn" onClick={() => testWebhook.mutate()} disabled={testWebhook.isPending || !form.webhook_url}>
              Send Test Event
            </button>
            {webhookTest && (
              <span style={{ fontSize: 13, color: webhookTest.startsWith("✔") ? "var(--success)" : "var(--danger)" }}>
                {webhookTest}
              </span>
            )}
          </div>
        </div>
      </div>
      {browsing && (
        <FolderBrowser
          onPick={(path) => {
            setForm({ ...form, downloads_dir: path });
            setBrowsing(false);
          }}
          onClose={() => setBrowsing(false)}
        />
      )}
    </>
  );
}
