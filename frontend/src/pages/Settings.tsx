import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../api/client";
import type { RootFolder, Settings as SettingsType } from "../api/types";
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
            Priority order decides which source is grabbed from first when a chapter is available in
            several places.
          </p>
          <div className="form-row">
            <label>Source priority</label>
            {text("source_priority")}
          </div>
          <div className="form-row">
            <label>MangaDex enabled</label>
            <Toggle on={form.source_mangadex_enabled === "true"} onChange={setBool("source_mangadex_enabled")} />
          </div>
          <div className="form-row">
            <label>WeebCentral enabled</label>
            <Toggle on={form.source_weebcentral_enabled === "true"} onChange={setBool("source_weebcentral_enabled")} />
          </div>
          <div className="form-row">
            <label>TCB Scans enabled</label>
            <Toggle on={form.source_tcbscans_enabled === "true"} onChange={setBool("source_tcbscans_enabled")} />
          </div>
          <div className="form-row">
            <label>Asura Scans enabled</label>
            <Toggle on={form.source_asura_enabled === "true"} onChange={setBool("source_asura_enabled")} />
          </div>
          <div className="form-row">
            <label>MangaPlus enabled</label>
            <Toggle on={form.source_mangaplus_enabled === "true"} onChange={setBool("source_mangaplus_enabled")} />
            <span style={{ color: "var(--text-faint)", fontSize: 13 }}>
              Official same-day Shonen Jump. Needs a residential IP — bans datacenters.
            </span>
          </div>
          <div className="form-row">
            <label>Nyaa (torrents) enabled</label>
            <Toggle on={form.source_nyaa_enabled === "true"} onChange={setBool("source_nyaa_enabled")} />
          </div>
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
      </div>
    </>
  );
}
