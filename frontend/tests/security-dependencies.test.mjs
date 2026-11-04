import assert from "node:assert/strict";
import { mkdtemp, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { test } from "node:test";
import { SourceMapConsumer, SourceNode } from "source-map-js";
import { createServer } from "vite";

const basicMap = {
  version: 3, sources: ["chapter.js"], names: [], mappings: "AAAA", sourcesContent: ["x"],
};
const indexedMap = (line, map = basicMap) => ({
  version: 3, sections: [{ offset: { line, column: 0 }, map }],
});

test("source maps reject unsafe section offsets before processing generated code", () => {
  // Malicious offsets previously reached a potentially unbounded loop. Test
  // rejection at construction, without running that loop on vulnerable code.
  for (const line of [1e12, Infinity, -1, 0.5]) {
    assert.throws(() => new SourceMapConsumer(indexedMap(line)), undefined, `offset ${line}`);
  }
  assert.throws(() => new SourceMapConsumer(indexedMap(6e6, indexedMap(6e6))));
  assert.equal(
    SourceNode.fromStringWithSourceMap("x\n", new SourceMapConsumer(indexedMap(0))).toString(),
    "x\n",
  );
});

test("development server denies private files through the WebAssembly request path", async () => {
  const root = await mkdtemp(join(tmpdir(), "mangarr-vite-security-"));
  let server;
  try {
    await writeFile(join(root, ".env"), "SYNTHETIC_TEST_ONLY=not-a-secret\n");
    await writeFile(join(root, "control.txt"), "ordinary public file\n");
    server = await createServer({
      root, configFile: false, logLevel: "silent", appType: "custom",
      server: { host: "127.0.0.1", port: 0 },
    });
    await server.listen();
    const base = `http://127.0.0.1:${server.httpServer.address().port}`;
    const denied = await fetch(`${base}/.env?vite-wasm-instance`);
    assert.equal(denied.status, 403);
    // A vulnerable server exposes the first four bytes in a WASM error.
    assert.doesNotMatch(await denied.text(), /SYNTH|53 59 4e 54/i);
    const control = await fetch(`${base}/control.txt`);
    assert.equal(control.status, 200);
    assert.equal(await control.text(), "ordinary public file\n");
  } finally {
    await server?.close();
    await rm(root, { recursive: true, force: true });
  }
});
