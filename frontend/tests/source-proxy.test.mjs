import assert from "node:assert/strict";
import { after, test } from "node:test";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { createServer } from "vite";

const server = await createServer({ server: { middlewareMode: true }, appType: "custom" });
after(() => server.close());
const { SourcePriority, ProxySettings, SourceRequestProxySettings } =
  await server.ssrLoadModule("/src/pages/Settings.tsx");

const base = {
  source_priority: "mangafire,weebcentral,mangadex",
  source_mangafire_enabled: "true",
  source_weebcentral_enabled: "true",
  source_mangadex_enabled: "true",
  source_mangafire_proxy_enabled: "false",
  source_weebcentral_proxy_enabled: "true",
  source_mangadex_proxy_enabled: "false",
};
const render = (Component, form) => renderToStaticMarkup(
  createElement(Component, { form, setForm() {}, set: () => () => {} }),
);
const tag = (html, name, id) => html.match(new RegExp(`<${name}[^>]*id="${id}"[^>]*>`))?.[0] ?? "";

test("existing settings keep direct source requests until explicit opt-in", () => {
  const html = render(SourceRequestProxySettings, base);
  assert.equal((html.match(/value="inherit" selected=""/g) ?? []).length, 2);
  assert.equal((html.match(/value="all" selected=""/g) ?? []).length, 0);
  assert.match(html, /page manifests, and images/);
  assert.match(html, /frontend and scripts, signing/);
  const priority = render(SourcePriority, base);
  assert.equal((priority.match(/class="priority-row/g) ?? []).length, 3);
  assert.match(priority, /aria-label="Use proxy for WeebCentral content downloads" aria-pressed="true"/);
});

test("all-request mode requires a masked source URL and locks the content toggle on", () => {
  const form = { ...base, source_mangafire_proxy_mode: "all", source_mangafire_proxy_url: "••••••••" };
  const html = render(SourceRequestProxySettings, form);
  assert.match(html, /value="all" selected=""/);
  const input = tag(html, "input", "mangafire-proxy-url");
  assert.match(input, /type="password"/);
  assert.match(input, /required=""/);
  assert.match(input, /value="••••••••"/);
  assert.match(render(SourcePriority, form), /aria-label="Use proxy for MangaFire content downloads" aria-pressed="true" disabled=""/);
  assert.match(render(ProxySettings, form), /aria-label="Use proxy for MangaFire content downloads" aria-pressed="true" disabled=""/);
});

test("disabled sources retain their route but cannot be edited or accidentally enabled", () => {
  const form = { ...base, source_mangafire_enabled: "false", source_mangafire_proxy_mode: "all" };
  const html = render(SourceRequestProxySettings, form);
  assert.match(tag(html, "select", "mangafire-proxy-mode"), /disabled=""/);
  assert.match(tag(html, "input", "mangafire-proxy-url"), /disabled=""/);
  assert.doesNotMatch(tag(html, "input", "mangafire-proxy-url"), /required=/);
  assert.match(html, /value="all" selected=""/);
  const priority = render(SourcePriority, form);
  assert.equal((priority.match(/class="priority-row/g) ?? []).length, 3);
  assert.doesNotMatch(priority, /mangafire_proxy/);
  assert.doesNotMatch(render(ProxySettings, form), /Use proxy for MangaFire content downloads/);
});
