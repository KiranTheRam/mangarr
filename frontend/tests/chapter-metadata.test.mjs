import assert from "node:assert/strict";
import { after, test } from "node:test";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { createServer } from "vite";

const server = await createServer({ server: { middlewareMode: true, hmr: false }, appType: "custom" });
after(() => server.close());
const { ChapterMetadataModal } = await server.ssrLoadModule("/src/pages/SeriesDetail.tsx");

test("chapter and ComicInfo saves have separate controls and archive availability", () => {
  const client = new QueryClient();
  const chapter = {
    id: 2, number: 1, title: "Chapter title", volume: 1, downloaded: true,
    file_path: "/library/Tracked/Chapter 1.cbz",
  };
  const render = () => renderToStaticMarkup(createElement(QueryClientProvider, { client },
    createElement(ChapterMetadataModal, { seriesId: 1, seriesTitle: "Tracked", chapter }),
  ));

  const beforeLoad = render();
  assert.match(beforeLoad, /<button class="btn primary">Save Mangarr metadata<\/button>/);
  assert.doesNotMatch(beforeLoad, /Save ComicInfo/);

  client.setQueryData(["comicinfo", 1, 2], {
    revision: "reviewed-archive", file_path: chapter.file_path, exists: true,
    title: "Archive title", actual_page_count: 1,
  });
  const loaded = render();
  assert.match(loaded, /value="Chapter title"/);
  assert.match(loaded, /value="Archive title"/);
  assert.match(loaded, /<button class="btn primary">Save Mangarr metadata<\/button>/);
  assert.match(loaded, /<button class="btn primary" disabled="">Save ComicInfo<\/button>/);
});
