import assert from "node:assert/strict";
import { after, test } from "node:test";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { createServer } from "vite";

const server = await createServer({ server: { middlewareMode: true, hmr: false }, appType: "custom" });
after(() => server.close());
const { TorznabSettings } = await server.ssrLoadModule("/src/pages/Settings.tsx");
const render = (form) => renderToStaticMarkup(createElement(TorznabSettings, { form, setForm() {} }));

test("Torznab starts optional and scheduled torrent fallback is off", () => {
  const html = render({ torznab_indexers: "[]", torrent_query_templates: "{series}" });
  assert.match(html, /Add Torznab indexer/);
  assert.match(html, /aria-label="Search torrents for missing monitored chapters" aria-pressed="false"/);
  assert.match(html, /Default torrent query templates/);
  assert.match(html, /\{chapter:03d\}/);
  assert.match(html, /\{volume:02d\}/);
});

test("multiple feeds retain masked keys and their enabled flags", () => {
  const entry = { id: "one", name: "Prowlarr", url: "http://prowlarr/1/api", api_key: "••••••••", categories: "7030,7020", search_type: "search", enabled: true };
  const html = render({ torznab_indexers: JSON.stringify([entry, { ...entry, id: "two", name: "Jackett", enabled: false, search_type: "book" }]), torrent_monitor_enabled: "true" });
  assert.equal((html.match(/<fieldset/g) ?? []).length, 2);
  assert.match(html, /id="one-api_key" type="password"[^>]*value="••••••••"/);
  assert.match(html, /value="7030,7020"/);
  assert.match(html, /value="book" selected=""/);
  assert.match(html, /aria-label="Enable indexer" aria-pressed="false"/);
  assert.match(html, /aria-label="Search torrents for missing monitored chapters" aria-pressed="true"/);
});
