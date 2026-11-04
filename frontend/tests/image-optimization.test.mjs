import assert from "node:assert/strict";
import { after, test } from "node:test";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { createServer } from "vite";

const server = await createServer({ server: { middlewareMode: true, hmr: false }, appType: "custom" });
after(() => server.close());
const { ImageOptimizationSettings } = await server.ssrLoadModule("/src/pages/Settings.tsx");
const render = (form) => renderToStaticMarkup(createElement(ImageOptimizationSettings, { form, setForm() {} }));
const tag = (html, id) => html.match(new RegExp(`<[^>]*id="${id}"[^>]*>`))?.[0] ?? "";

test("optimization is off with disabled controls for existing installations", () => {
  const html = render({});
  assert.match(html, /aria-label="Optimize downloaded images" aria-pressed="false"/);
  for (const id of ["image-format", "image-quality", "image-width"])
    assert.match(tag(html, id), /disabled=""/);
  assert.match(tag(html, "image-quality"), /value="85"/);
  assert.match(tag(html, "image-width"), /value="0"/);
  assert.match(html, /Original quality is the default/);
  assert.match(html, /Original quality \(default\)/);
  assert.match(html, /Optimization is off/);
  assert.match(html, /aria-label="Quality slider"[^>]*disabled=""/);
});

test("enabled controls expose limits, saved policy and resize precedence", () => {
  const html = render({ image_optimization_enabled: "true", image_optimization_format: "jpeg", image_optimization_quality: "90", image_optimization_max_width: "1600" });
  assert.match(html, /value="jpeg" selected=""/);
  assert.match(tag(html, "image-quality"), /min="1" max="100" step="1"/);
  assert.match(tag(html, "image-width"), /value="1600"/);
  assert.doesNotMatch(tag(html, "image-width"), /disabled=/);
  assert.match(html, /unless resizing is required/);
  assert.match(html, /settings saved when it starts/);
  assert.match(html, /even 100 is lossy/);
  assert.match(html, /Live quality preview/);
  assert.match(html, /View at actual size \(100%\)/);
  assert.match(html, /Changes update automatically without saving/);
  assert.doesNotMatch(html.match(/<input[^>]*aria-label="Quality slider"[^>]*>/)?.[0] ?? "", /disabled=/);
});
