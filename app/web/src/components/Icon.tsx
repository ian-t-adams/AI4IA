// In-house stroke icons on a 24px grid. They inherit currentColor, so every
// theme and a user-chosen accent colour them correctly, and they are hidden from
// assistive technology: the control that contains an icon carries the name.
import type { ReactNode } from "react";

const PATHS = {
  chat: <path d="M4 6.5A2.5 2.5 0 0 1 6.5 4h11A2.5 2.5 0 0 1 20 6.5v8a2.5 2.5 0 0 1-2.5 2.5H11l-4.5 3.5V17h0A2.5 2.5 0 0 1 4 14.5z" />,
  library: (
    <>
      <path d="M5 4h3.5v16H5z" />
      <path d="M10.5 4H14v16h-3.5z" />
      <path d="M16 5.2l3.2-.9 3.3 14.6-3.2.9z" />
    </>
  ),
  avatar: (
    <>
      <rect x="3.5" y="3.5" width="17" height="17" rx="3" />
      <circle cx="12" cy="10" r="3.2" />
      <path d="M6.8 20.2a5.6 5.6 0 0 1 10.4 0" />
    </>
  ),
  studio: (
    <>
      <rect x="4" y="4" width="6.5" height="6.5" rx="1.2" />
      <rect x="13.5" y="4" width="6.5" height="6.5" rx="1.2" />
      <rect x="4" y="13.5" width="6.5" height="6.5" rx="1.2" />
      <path d="M16.75 13.5v6.5M13.5 16.75H20" />
    </>
  ),
  inbox: (
    <>
      <path d="M4 13.5 6.2 5.5h11.6L20 13.5V18a1.5 1.5 0 0 1-1.5 1.5h-13A1.5 1.5 0 0 1 4 18z" />
      <path d="M4 13.5h4.5l1.5 2.5h4l1.5-2.5H20" />
    </>
  ),
  settings: (
    <>
      <circle cx="12" cy="12" r="3" />
      <path d="M12 3.5v2.3M12 18.2v2.3M3.5 12h2.3M18.2 12h2.3M6 6l1.6 1.6M16.4 16.4 18 18M6 18l1.6-1.6M16.4 7.6 18 6" />
      <circle cx="12" cy="12" r="6.2" />
    </>
  ),
  help: (
    <>
      <circle cx="12" cy="12" r="8.5" />
      <path d="M9.6 9.6a2.5 2.5 0 1 1 3.4 2.3c-.6.3-1 .8-1 1.5v.6" />
      <path d="M12 16.9h.01" />
    </>
  ),
  status: <path d="M3 12h4l2.5-6 5 12 2.5-6H21" />,
  docs: (
    <>
      <path d="M3.5 6c2.6-1.4 5.8-1.4 8.5.6 2.7-2 5.9-2 8.5-.6v12.5c-2.6-1.4-5.8-1.4-8.5.6-2.7-2-5.9-2-8.5-.6z" />
      <path d="M12 6.6v12.5" />
    </>
  ),
  admin: <path d="M12 3.5 19 6.3v5.2c0 4.4-2.9 7.7-7 9-4.1-1.3-7-4.6-7-9V6.3z" />,
  account: (
    <>
      <circle cx="12" cy="8.5" r="3.8" />
      <path d="M4.8 20a7.2 7.2 0 0 1 14.4 0" />
    </>
  ),
  menu: <path d="M4 7h16M4 12h16M4 17h16" />,
  plus: <path d="M12 5v14M5 12h14" />,
  search: (
    <>
      <circle cx="11" cy="11" r="6.2" />
      <path d="m20 20-4.4-4.4" />
    </>
  ),
  close: <path d="M6 6l12 12M18 6 6 18" />,
  "chevron-down": <path d="m6.5 9.5 5.5 5.5 5.5-5.5" />,
  "chevron-left": <path d="M14.5 6.5 9 12l5.5 5.5" />,
  "chevron-right": <path d="M9.5 6.5 15 12l-5.5 5.5" />,
  "panel-left": (
    <>
      <rect x="3.5" y="4.5" width="17" height="15" rx="2" />
      <path d="M9.5 4.5v15" />
    </>
  ),
  sliders: (
    <>
      <path d="M4 7h9M17 7h3M4 17h3M11 17h9" />
      <circle cx="15" cy="7" r="2" />
      <circle cx="9" cy="17" r="2" />
    </>
  ),
  mic: (
    <>
      <rect x="9" y="3.5" width="6" height="11" rx="3" />
      <path d="M5.5 11a6.5 6.5 0 0 0 13 0M12 17.5V21" />
    </>
  ),
  stop: <rect x="7" y="7" width="10" height="10" rx="1.6" />,
  send: <path d="M12 19V5.5M6.5 11 12 5.5l5.5 5.5" />,
  attach: <path d="M19.5 11.5 12 19a5 5 0 0 1-7.1-7.1l8.2-8.2a3.4 3.4 0 0 1 4.8 4.8l-8.2 8.2a1.8 1.8 0 0 1-2.6-2.6L14.6 6.6" />,
  expand: <path d="M14 4h6v6M10 20H4v-6M20 4l-6.5 6.5M4 20l6.5-6.5" />,
  shrink: <path d="M4 14h6v6M20 10h-6V4M14 10l6-6M4 20l6-6" />,
  fill: (
    <>
      <rect x="3.5" y="5.5" width="17" height="13" rx="1.6" />
      <path d="M8 9.5 6 12l2 2.5M16 9.5l2 2.5-2 2.5" />
    </>
  ),
  fit: (
    <>
      <rect x="3.5" y="5.5" width="17" height="13" rx="1.6" />
      <path d="M8.5 9.5h7v5h-7z" />
    </>
  ),
  fullscreen: (
    <>
      <path d="M4 8.5V4h4.5M15.5 4H20v4.5M20 15.5V20h-4.5M8.5 20H4v-4.5" />
    </>
  ),
  copy: (
    <>
      <rect x="8.5" y="8.5" width="11" height="12" rx="1.8" />
      <path d="M5.5 15.5V5.3A1.8 1.8 0 0 1 7.3 3.5h8.2" />
    </>
  ),
  check: <path d="m5 12.5 4.5 4.5L19 7.5" />,
  speaker: (
    <>
      <path d="M4 9.5h3.5L12 5.5v13l-4.5-4H4z" />
      <path d="M15.5 9a4 4 0 0 1 0 6M18 6.5a7.6 7.6 0 0 1 0 11" />
    </>
  ),
  pencil: (
    <>
      <path d="M4 20l1-4.5L16.3 4.2a2.1 2.1 0 0 1 3 3L8 18.5z" />
      <path d="m14 6.5 3 3" />
    </>
  ),
  trash: (
    <>
      <path d="M4.5 7h15M9.5 7V4.5h5V7" />
      <path d="M6.5 7l.9 12.5h9.2l.9-12.5" />
    </>
  ),
  external: (
    <>
      <path d="M14 4h6v6M20 4l-8.5 8.5" />
      <path d="M18 14v4.5a1.5 1.5 0 0 1-1.5 1.5h-11A1.5 1.5 0 0 1 4 18.5v-11A1.5 1.5 0 0 1 5.5 6H10" />
    </>
  ),
  "sign-out": (
    <>
      <path d="M14.5 4H18a2 2 0 0 1 2 2v12a2 2 0 0 1-2 2h-3.5" />
      <path d="M10 8l-4 4 4 4M6 12h10" />
    </>
  ),
  "arrow-down": <path d="M12 5v14M6 13l6 6 6-6" />,
  "arrow-left": <path d="M19 12H5M11 6l-6 6 6 6" />,
  keyboard: (
    <>
      <rect x="3" y="6.5" width="18" height="11" rx="2" />
      <path d="M7 10.5h.01M11 10.5h.01M15 10.5h.01M8 14h8" />
    </>
  ),
  image: (
    <>
      <rect x="3.5" y="4.5" width="17" height="15" rx="2" />
      <circle cx="9" cy="10" r="1.6" />
      <path d="m4 17 5-4.5 4 3.5 3-2.5 4 3.5" />
    </>
  ),
  wave: <path d="M4 10v4M8 7v10M12 4v16M16 7v10M20 10v4" />,
} satisfies Record<string, ReactNode>;

export type IconName = keyof typeof PATHS;

export function Icon({
  name,
  size = 20,
  className,
}: {
  name: IconName;
  size?: number;
  className?: string;
}) {
  return (
    <svg
      className={className}
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth={1.8}
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
      focusable="false"
    >
      {PATHS[name]}
    </svg>
  );
}
