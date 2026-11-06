// Shared 24px artwork from the approved icon proposal. Icons inherit semantic colors.
const artwork = {
  "activity": <><path d="M7 4v14m-3-3 3 3 3-3M17 20V6m-3 3 3-3 3 3"/></>,
  "add": <><path d="M12 5v14M5 12h14"/></>,
  "arrow-down": <><path d="M12 5v14m-5-5 5 5 5-5"/></>,
  "arrow-left": <><path d="M19 12H5m5-5-5 5 5 5"/></>,
  "arrow-right": <><path d="M5 12h14m-5-5 5 5-5 5"/></>,
  "arrow-up": <><path d="M12 19V5M7 10l5-5 5 5"/></>,
  "bookmark-off": <><path d="M7 4h10v16l-5-3-5 3zM5 5l14 14"/></>,
  "bookmark": <><path d="M7 4h10v16l-5-3-5 3z"/></>,
  "calendar": <><rect x="4" y="6" width="16" height="14" rx="2"/><path d="M8 4v4M16 4v4M4 10h16M8 14h2M14 14h2M8 17h2"/></>,
  "check": <><path d="m5 12 4.5 4.5L19 7"/></>,
  "chevron-down": <><path d="m5 9 7 7 7-7"/></>,
  "chevron-right": <><path d="m9 5 7 7-7 7"/></>,
  "cleanup": <><path d="M18 4 9 14m-2-2 5 5-3 3H4l-1-4zM5 17l3 3"/></>,
  "close": <><path d="M6 6l12 12M18 6 6 18"/></>,
  "download": <><path d="M12 4v11m-4-4 4 4 4-4M5 17v3h14v-3"/></>,
  "files": <><path d="M7 4h7l4 4v12H7zM14 4v4h4M10 12h5m-5 3h5"/></>,
  "folder-open": <><path d="M3 18V6a2 2 0 0 1 2-2h5l2 2h6a2 2 0 0 1 2 2v2M3 19l2.2-8h16l-2.2 8z"/></>,
  "folder-up": <><path d="M3 8a2 2 0 0 1 2-2h5l2 2h7a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2zM12 17v-6m-3 3 3-3 3 3"/></>,
  "folder": <><path d="M3 7a2 2 0 0 1 2-2h5l2 2h7a2 2 0 0 1 2 2v9a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/></>,
  "history": <><path d="M4 11a8 8 0 1 1 2.2 6.2M4 5v6h6M12 7v5l3 2"/></>,
  "library": <><rect x="4" y="5" width="4" height="14" rx="0.6"/><rect x="10" y="5" width="4" height="14" rx="0.6"/><rect x="16" y="5" width="4" height="14" rx="0.6" transform="rotate(-8 18 12)"/></>,
  "link": <><path d="m10 14 4-4M8 16H6a4 4 0 0 1 0-8h3m6 0h3a4 4 0 1 1 0 8h-3"/></>,
  "refresh": <><path d="M19 9a7.5 7.5 0 0 0-13-2L4 9m0-5v5h5M5 15a7.5 7.5 0 0 0 13 2l2-2m0 5v-5h-5"/></>,
  "rename": <><path d="m5 16-.5 3.5L8 19 19 8l-3-3zM14.5 6.5l3 3M5 21h14"/></>,
  "scan": <><rect x="4" y="5" width="16" height="14" rx="2"/><path d="M7 9h10M7 15h4m5 0h1"/></>,
  "search-missing": <><circle cx="10" cy="10" r="5.5"/><path d="m14.5 14.5 5 5M10 7.5v5m-2.5-2.5h5"/></>,
  "search": <><circle cx="10.5" cy="10.5" r="5.5"/><path d="m15 15 4.5 4.5"/></>,
  "settings": <><path d="M10 3h4l.5 2.2 2 .9 1.9-1.1 2.8 2.8-1.1 1.9.9 2L23 12v2l-2.2.5-.9 2 1.1 1.9-2.8 2.8-1.9-1.1-2 .9L14 23h-4l-.5-2.2-2-.9-1.9 1.1-2.8-2.8 1.1-1.9-.9-2L1 14v-2l2.2-.5.9-2L3 7.6l2.8-2.8 1.9 1.1 2-.9z" transform="translate(.96 .04) scale(.92)"/><circle cx="12" cy="12" r="3"/></>,
  "wanted": <><circle cx="11" cy="11" r="6"/><path d="m15.5 15.5 4 4M11 8v3m0 3h.01"/></>,
  "warning": <><path d="M12 4 21 20H3zM12 9v5m0 3h.01"/></>,
};

export type IconName = keyof typeof artwork;

/** Decorative artwork: the surrounding label or control supplies the accessible name. */
export function Icon({ name, size = 20, className = "" }: { name: IconName; size?: number; className?: string }) {
  return <svg className={`ui-icon ${className}`.trim()} width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true" focusable="false">{artwork[name]}</svg>;
}
