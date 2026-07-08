import DOMPurify from "dompurify";

/** Series descriptions come from third-party metadata providers (AniList,
 * MangaUpdates) — community-editable content that must never run script in a
 * UI that holds the API key. Allow only basic formatting. */
export function sanitizeDescription(html: string): string {
  return DOMPurify.sanitize(html, {
    ALLOWED_TAGS: [
      "a", "b", "strong", "i", "em", "u", "s", "br", "p", "span",
      "ul", "ol", "li", "blockquote",
    ],
    ALLOWED_ATTR: ["href"],
  });
}
