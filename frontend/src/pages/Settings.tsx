import { useEffect, useState, type ChangeEvent } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../api/client";
import type {
  ApiKey,
  KavitaTestResult,
  RootFolder,
  Settings as SettingsType,
} from "../api/types";
import { FolderBrowser } from "../components/FolderBrowser";
import { ErrorNotice, Spinner, Toggle, Toolbar } from "../components/common";
import { SOURCE_LABELS } from "../sources";

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

const SOURCE_HINTS: Record<string, string> = {
  mangaplus: "Official same-day Shonen Jump. Needs a residential IP — bans datacenters.",
  webtoons: "Official WEBTOON Originals. Only episodes that are currently free (no coins) are listed.",
  mangafire: "Broad English archive coverage, including many bonus and decimal chapters.",
  atsumaru: "Broad archive of manga, manhwa, and manhua; picks the best-rated scanlation of each chapter.",
  viz: "Exact printed-volume mappings for VIZ-licensed series; does not download chapters.",
  wikipedia: "Chapter titles and printed-volume tables for publishers not covered by VIZ.",
  nyaa: "Sent to the download client below and imported when complete.",
};

const CONTENT_SOURCES = new Set([
  "mangaplus",
  "webtoons",
  "mangadex",
  "mangafire",
  "weebcentral",
  "atsumaru",
  "tcbscans",
  "asura",
]);

function orderedSourceNames(form: SettingsType): string[] {
  const known = Object.keys(form)
    .filter(
      (key) =>
        key.startsWith("source_") &&
        key.endsWith("_enabled") &&
        !key.endsWith("_proxy_enabled"),
    )
    .map((key) => key.slice("source_".length, -"_enabled".length));
  const order = (form.source_priority ?? "")
    .split(",")
    .map((source) => source.trim())
    .filter(
      (source, index, sources) =>
        source && known.includes(source) && sources.indexOf(source) === index,
    );
  for (const source of known) if (!order.includes(source)) order.push(source);
  return order;
}

/** Ordered, toggleable source list — stored as the comma-separated
 * source_priority setting, but never hand-edited as text. */
export function SourcePriority({
  form,
  setForm,
}: {
  form: SettingsType;
  setForm: (f: SettingsType) => void;
}) {
  const order = orderedSourceNames(form);

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
            <span className="priority-hint">{SOURCE_HINTS[name] ?? ""}</span>
            {CONTENT_SOURCES.has(name) && (
              <span className="priority-toggle priority-proxy-toggle" title="Proxy chapter page/image downloads">
                <span>Proxy</span>
                <Toggle
                  on={form[`source_${name}_proxy_mode`] === "all" || form[`source_${name}_proxy_enabled`] === "true"}
                  disabled={!enabled || form[`source_${name}_proxy_mode`] === "all"}
                  label={`Use proxy for ${SOURCE_LABELS[name] ?? name} content downloads`}
                  onChange={(proxyEnabled) =>
                    setForm({
                      ...form,
                      [`source_${name}_proxy_enabled`]: proxyEnabled ? "true" : "false",
                    })
                  }
                />
              </span>
            )}
            <span className="priority-toggle">
              <span>Enabled</span>
              <Toggle
                on={enabled}
                label={`${enabled ? "Disable" : "Enable"} ${SOURCE_LABELS[name] ?? name}`}
                onChange={(v) => {
                  const next = {
                    ...form,
                    [`source_${name}_enabled`]: v ? "true" : "false",
                  };
                  setForm(next);
                }}
              />
            </span>
          </div>
        );
      })}
    </div>
  );
}

export function ProxySettings({
  form,
  setForm,
  set,
}: {
  form: SettingsType;
  setForm: (form: SettingsType) => void;
  set: (key: string) => (event: ChangeEvent<HTMLInputElement | HTMLSelectElement>) => void;
}) {
  const eligibleSources = orderedSourceNames(form).filter(
    (name) => CONTENT_SOURCES.has(name) && form[`source_${name}_enabled`] === "true",
  );

  return (
    <div className="settings-section proxy-settings mobile-proxy-settings">
      <h3>Proxy</h3>
      <p className="section-hint">
        Route chapter page and image downloads through a shared HTTP(S) proxy. Source requests
        stay direct unless you explicitly select all requests below.
      </p>
      <div className="form-row">
        <label>Proxy address</label>
        <input
          type="password"
          autoComplete="new-password"
          inputMode="url"
          autoCapitalize="none"
          autoCorrect="off"
          placeholder="http://proxy.example:8080"
          value={form.download_proxy_url ?? ""}
          onChange={set("download_proxy_url")}
        />
      </div>
      <div className="proxy-source-list" aria-label="Sources using the download proxy">
        <div className="proxy-source-list-heading">Use proxy for content downloads from</div>
        {eligibleSources.map((name) => {
          const label = SOURCE_LABELS[name] ?? name;
          return (
            <div className="proxy-source-row" key={name}>
              <div className="proxy-source-copy">
                <span className="proxy-source-name">{label}</span>
                <span className="proxy-source-detail">
                  {form[`source_${name}_proxy_mode`] === "all"
                    ? "All requests use the source proxy below"
                    : form[`source_${name}_proxy_enabled`] === "true"
                    ? "Downloads fail closed through the shared proxy"
                    : "Downloads connect directly"}
                </span>
              </div>
              <Toggle
                on={form[`source_${name}_proxy_mode`] === "all" || form[`source_${name}_proxy_enabled`] === "true"}
                disabled={form[`source_${name}_proxy_mode`] === "all"}
                label={`Use proxy for ${label} content downloads`}
                onChange={(enabled) =>
                  setForm({
                    ...form,
                    [`source_${name}_proxy_enabled`]: enabled ? "true" : "false",
                  })
                }
              />
            </div>
          );
        })}
        {eligibleSources.length === 0 && (
          <p className="section-hint proxy-empty">
            Enable a content source in Sources to make it eligible for the proxy.
          </p>
        )}
      </div>
      <p className="section-hint proxy-fail-closed-note">
        Proxied downloads fail closed: if the proxy is unavailable, Mangarr does not retry that
        request directly. The all-requests mode below overrides the content toggle for that source.
      </p>
    </div>
  );
}

/** Broader routing is deliberately independent of existing content-only settings. */
export function SourceRequestProxySettings({ form, set }: {
  form: SettingsType;
  set: (key: string) => (event: ChangeEvent<HTMLInputElement | HTMLSelectElement>) => void;
}) {
  return (
    <div className="settings-section">
      <h3>Source request proxies</h3>
      <p className="section-hint">
        MangaFire and WeebCentral can use a separate proxy for all source requests: search,
        frontend and scripts, signing, chapters, page manifests, and images. Existing content
        proxy settings apply until you opt in here. A proxy cannot guarantee access to a site.
        Saved addresses are hidden because they may contain credentials.
      </p>
      {["mangafire", "weebcentral"].map((name) => (
        <div key={name}>
          <div className="form-row">
            <label htmlFor={`${name}-proxy-mode`}>{SOURCE_LABELS[name]} routing</label>
            <select
              id={`${name}-proxy-mode`}
              value={form[`source_${name}_proxy_mode`] ?? "inherit"}
              disabled={form[`source_${name}_enabled`] !== "true"}
              onChange={set(`source_${name}_proxy_mode`)}
            >
              <option value="inherit">Direct source requests; use content toggle for images</option>
              <option value="all">Proxy all source requests and images</option>
            </select>
          </div>
          <div className="form-row">
            <label htmlFor={`${name}-proxy-url`}>{SOURCE_LABELS[name]} proxy URL</label>
            <input
              id={`${name}-proxy-url`} type="password" autoComplete="new-password"
              autoCapitalize="none" autoCorrect="off" placeholder="http://proxy.example:8080"
              disabled={form[`source_${name}_enabled`] !== "true"}
              required={form[`source_${name}_proxy_mode`] === "all" && form[`source_${name}_enabled`] === "true"}
              value={form[`source_${name}_proxy_url`] ?? ""}
              onChange={set(`source_${name}_proxy_url`)}
            />
          </div>
        </div>
      ))}
      <p className="section-hint">
        All requests uses only the source URL above, including images, and fails closed if it
        is missing or unavailable. Saving changes affects new requests and jobs; an active
        download keeps its original route. Disabled sources retain their settings.
      </p>
    </div>
  );
}

function parseLibraryMap(raw: string | undefined): Record<string, number> {
  if (!raw) return {};
  try {
    const parsed = JSON.parse(raw);
    return parsed && typeof parsed === "object" && !Array.isArray(parsed) ? parsed : {};
  } catch {
    return {};
  }
}

/** Kavita connection: credentials, scan granularity, and which Kavita library
 * each mangarr root folder feeds. Libraries come from the live server so the
 * mapping is a pick-list rather than hand-typed ids. */
function KavitaSettings({
  form,
  setForm,
  set,
  setBool,
  savedUrl,
}: {
  form: SettingsType;
  setForm: (f: SettingsType) => void;
  set: (key: string) => (e: ChangeEvent<HTMLInputElement | HTMLSelectElement>) => void;
  setBool: (key: string) => (v: boolean) => void;
  savedUrl: string;
}) {
  const [testResult, setTestResult] = useState<string | null>(null);
  const { data: rootFolders } = useQuery({
    queryKey: ["rootfolders"],
    queryFn: () => api.get<RootFolder[]>("/rootfolders"),
  });

  // Load libraries from the saved connection (not the in-progress form) so
  // typing in the URL field does not fire a request per keystroke.
  const configured = form.kavita_enabled === "true" && !!savedUrl;
  const { data: loaded } = useQuery({
    queryKey: ["kavita-libraries", savedUrl],
    queryFn: () => api.post<KavitaTestResult>("/settings/kavita/test", { url: savedUrl }),
    enabled: configured,
    retry: false,
    staleTime: Infinity,
  });

  const [tested, setTested] = useState<KavitaTestResult | null>(null);
  const test = useMutation({
    mutationFn: () =>
      api.post<KavitaTestResult>("/settings/kavita/test", {
        url: form.kavita_url,
        api_key: form.kavita_api_key,
      }),
    onSuccess: (d) => {
      setTested(d);
      setTestResult(`✔ Connected — Kavita ${d.version}, ${d.libraries.length} librar${d.libraries.length === 1 ? "y" : "ies"}`);
    },
    onError: (e) => setTestResult(`✖ ${(e as Error).message}`),
  });

  const libraries = tested?.libraries ?? loaded?.libraries ?? [];
  const map = parseLibraryMap(form.kavita_library_map);

  const setMapping = (rootId: number, libraryId: string) => {
    const next = { ...map };
    if (libraryId) next[String(rootId)] = Number(libraryId);
    else delete next[String(rootId)];
    setForm({
      ...form,
      kavita_library_map: Object.keys(next).length ? JSON.stringify(next) : "",
    });
  };

  return (
    <div className="settings-section">
      <h3>Connect — Kavita</h3>
      <p className="section-hint">
        Trigger a Kavita scan after chapters are imported so new downloads appear in the reader
        right away. Create an API key in Kavita under Settings → Account → API Key.
      </p>
      <div className="form-row">
        <label>Enabled</label>
        <Toggle label="Enable Kavita" on={form.kavita_enabled === "true"} onChange={setBool("kavita_enabled")} />
      </div>
      <div className="form-row">
        <label>URL</label>
        <input
          type="text"
          inputMode="url"
          autoCapitalize="none"
          autoCorrect="off"
          placeholder="http://kavita:5000"
          value={form.kavita_url ?? ""}
          onChange={set("kavita_url")}
        />
      </div>
      <div className="form-row">
        <label>API key</label>
        <input type="password" value={form.kavita_api_key ?? ""} onChange={set("kavita_api_key")} />
      </div>
      <p className="section-hint">Re-enter the API key after changing the URL, before testing or saving.</p>
      <div className="form-row">
        <label>Scan scope</label>
        <select value={form.kavita_scan_mode ?? "series"} onChange={set("kavita_scan_mode")}>
          <option value="series">Series (falls back to library for new series)</option>
          <option value="library">Whole library every time</option>
        </select>
      </div>
      <p className="section-hint">
        A series scan only walks the affected series folder. Kavita cannot scan a series it has
        never seen, so the first import for a newly added series scans its whole library — as does
        a title Kavita holds more than once, since guessing between them could scan the wrong one.
      </p>
      <div className="form-row">
        <label></label>
        <button
          className="btn"
          onClick={() => test.mutate()}
          disabled={test.isPending || !form.kavita_url}
        >
          Test Connection
        </button>
        {testResult && (
          <span
            style={{ fontSize: 13, color: testResult.startsWith("✔") ? "var(--success)" : "var(--danger)" }}
          >
            {testResult}
          </span>
        )}
      </div>
      <h4 style={{ margin: "16px 0 4px", fontSize: 14 }}>Library mapping</h4>
      <p className="section-hint">
        Which Kavita library each root folder belongs to. Leave on “Match by path” unless mangarr
        and Kavita mount the library at paths with nothing in common.
      </p>
      {libraries.length === 0 && (
        <p className="section-hint">
          Test the connection to load the libraries available for mapping.
        </p>
      )}
      {rootFolders?.map((rf) => (
        <div className="form-row" key={rf.id}>
          <label style={{ width: "auto", flex: 1, minWidth: 0, wordBreak: "break-all" }}>
            {rf.path}
          </label>
          <select
            value={map[String(rf.id)] ?? ""}
            onChange={(e) => setMapping(rf.id, e.target.value)}
            disabled={libraries.length === 0}
          >
            <option value="">Match by path</option>
            {libraries.map((lib) => (
              <option value={lib.id} key={lib.id}>
                {lib.name}
              </option>
            ))}
          </select>
        </div>
      ))}
    </div>
  );
}

export function ImageOptimizationSettings({ form, setForm }: {
  form: SettingsType;
  setForm: (form: SettingsType) => void;
}) {
  const enabled = form.image_optimization_enabled === "true";
  const change = (key: string) => (event: ChangeEvent<HTMLInputElement | HTMLSelectElement>) =>
    setForm({ ...form, [key]: event.target.value });
  return (
    <div className="settings-section">
      <h3>Chapter Image Optimization</h3>
      <p className="section-hint">
        Reduce storage for new direct downloads using lossy compression. Existing files and
        torrent imports are unchanged. Off keeps the original image bytes. Each download uses
        the settings saved when it starts.
      </p>
      <div className="form-row">
        <label>Optimize downloaded images</label>
        <Toggle on={enabled} onChange={(value) => setForm({ ...form,
          image_optimization_enabled: value ? "true" : "false" })}
          label="Optimize downloaded images" />
      </div>
      <div className="form-row">
        <label htmlFor="image-format">Output format</label>
        <select id="image-format" value={form.image_optimization_format ?? "webp"}
          onChange={change("image_optimization_format")} disabled={!enabled}>
          <option value="webp">WebP</option>
          <option value="jpeg">JPEG (wider reader compatibility)</option>
        </select>
      </div>
      <div className="form-row">
        <label htmlFor="image-quality">Quality (1–100)</label>
        <input id="image-quality" type="number" min="1" max="100" step="1"
          value={form.image_optimization_quality ?? "85"} disabled={!enabled}
          onChange={change("image_optimization_quality")} aria-describedby="image-quality-hint" />
      </div>
      <p id="image-quality-hint" className="section-hint">
        Higher quality usually uses more space. 85 is a starting point; even 100 is lossy.
        Check small text and line art before choosing lower quality.
      </p>
      <div className="form-row">
        <label htmlFor="image-width">Maximum width (pixels)</label>
        <input id="image-width" type="number" min="0" max="10000" step="1"
          value={form.image_optimization_max_width ?? "0"} disabled={!enabled}
          onChange={change("image_optimization_max_width")} aria-describedby="image-width-hint" />
      </div>
      <p id="image-width-hint" className="section-hint">
        0 keeps the original dimensions. Resizing keeps the aspect ratio and never upscales.
        Originals are kept when encoding would be larger, unless resizing is required.
        Animated, unsupported or oversized pages stay original; JPEG also keeps transparent
        pages original. The width limit applies only to pages that can be safely optimized.
        See the logs for skipped pages.
      </p>
    </div>
  );
}

export default function Settings() {
  const queryClient = useQueryClient();
  const { data: saved, isLoading, error: loadError, refetch: retryLoad } = useQuery({
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

  const dirty = !!saved && JSON.stringify(form) !== JSON.stringify(saved);
  useEffect(() => {
    if (!dirty) return;
    const warn = (event: BeforeUnloadEvent) => { event.preventDefault(); };
    window.addEventListener("beforeunload", warn);
    return () => window.removeEventListener("beforeunload", warn);
  }, [dirty]);

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

  if (loadError && !saved) return <><Toolbar title="Settings" /><ErrorNotice error={loadError} retry={() => void retryLoad()} /></>;

  if (isLoading || !saved) {
    return (
      <>
        <Toolbar title="Settings" />
        <Spinner />
      </>
    );
  }

  const set = (key: string) => (e: ChangeEvent<HTMLInputElement | HTMLSelectElement>) =>
    setForm({ ...form, [key]: e.target.value });
  const setBool = (key: string) => (v: boolean) => setForm({ ...form, [key]: v ? "true" : "false" });

  const text = (key: string, secret = false) => (
    <input type={secret ? "password" : "text"} value={form[key] ?? ""} onChange={set(key)} />
  );

  return (
    <>
      <Toolbar title="Settings">
        {dirty && <span role="status">Unsaved changes</span>}
        {save.isSuccess && !dirty && <span style={{ color: "var(--success)", fontSize: 13 }}>Saved</span>}
        <button className="btn primary" onClick={() => save.mutate()} disabled={save.isPending || !dirty}>
          {save.isPending ? "Saving…" : "Save Changes"}
        </button>
      </Toolbar>
      <div className="content">
        <ErrorNotice error={loadError} retry={() => void retryLoad()} />
        <ErrorNotice error={save.error} />
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
          <div className="form-row">
            <label>Finished series</label>
            <select value={form.finished_series_mode ?? "slow"} onChange={set("finished_series_mode")}>
              <option value="keep">Keep checking every pass</option>
              <option value="slow">Check less often</option>
              <option value="unmonitor">Unmonitor</option>
            </select>
          </div>
          {(form.finished_series_mode ?? "slow") === "slow" && (
            <div className="form-row">
              <label>Check finished series every (days)</label>
              {text("finished_series_check_days")}
            </div>
          )}
          <p className="section-hint">
            Applies once a series is finished or cancelled and every monitored chapter is on disk.
          </p>
        </div>

        <ImageOptimizationSettings form={form} setForm={setForm} />

        <div className="settings-section">
          <h3>Sources</h3>
          <p className="section-hint">
            Chapters are grabbed from the highest-priority enabled source that has them — use the
            arrows to reorder.
          </p>
          <div className="desktop-proxy-settings">
            <div className="form-row">
              <label>Content proxy URL</label>
              <input
                type="password"
                autoComplete="new-password"
                inputMode="url"
                autoCapitalize="none"
                autoCorrect="off"
                placeholder="http://proxy.example:8080"
                value={form.download_proxy_url ?? ""}
                onChange={set("download_proxy_url")}
              />
            </div>
            <p className="section-hint">
              The shared proxy applies to chapter page/image downloads for checked sources. Other
              requests stay direct unless all requests is selected below. Proxy failures never
              retry directly.
            </p>
          </div>
          <SourcePriority form={form} setForm={setForm} />
        </div>

        <ProxySettings form={form} setForm={setForm} set={set} />
        <SourceRequestProxySettings form={form} set={set} />

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
            Re-enter the password after changing the URL or username, before testing or saving.
          </p>
          <div className="form-row">
            <label>Enabled</label>
            <Toggle label="Enable qBittorrent" on={form.qbittorrent_enabled === "true"} onChange={setBool("qbittorrent_enabled")} />
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
            <label>Auto-grab maximum size (GiB)</label>
            {text("torrent_auto_max_size_gib")}
          </div>
          <div className="form-row">
            <label>Auto-grab minimum seeders</label>
            {text("torrent_auto_min_seeders")}
          </div>
          <p className="section-hint">
            During “Search now” setup, Mangarr reads Nyaa torrent metadata, chooses the single
            release covering the most missing chapters, and uses direct sources for anything the
            selected torrent does not cover.
          </p>
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

        <KavitaSettings
          form={form}
          setForm={setForm}
          set={set}
          setBool={setBool}
          savedUrl={saved.kavita_url ?? ""}
        />

        <div className="settings-section">
          <h3>Connect — Webhook</h3>
          <p className="section-hint">
            Notify a request manager (e.g. NextPanel) whenever chapters are imported, so requests
            flip to Available instantly. Point the URL at NextPanel's
            /api/v1/webhooks/mangarr endpoint and paste the same webhook secret configured there.
            Re-enter the secret after changing the URL, before testing or saving.
          </p>
          <div className="form-row">
            <label>Enabled</label>
            <Toggle label="Enable webhook" on={form.webhook_enabled === "true"} onChange={setBool("webhook_enabled")} />
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
