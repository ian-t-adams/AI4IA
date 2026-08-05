# AI4IA Product Design System

## Intent

AI4IA is a dense enterprise product used during focused knowledge work. Design
serves the task: a dark navigation shell frames a calm conversation surface, one
restrained accent communicates primary action and selection, and semantic colors
communicate state. The redesign improves information architecture and component
consistency without rebranding the product.

## Theme

- Preserve the existing light, dark, and high-contrast themes in
  `src/app/globals.css`.
- Preserve the dark navigation shell (`--bg-sidebar`) in every theme.
- Use `--bg` for the conversation canvas and `--bg-elevated` for toolbars,
  inspectors, menus, and controls.
- Use `--accent` only for primary actions, active selection, and links.
- Reserve the existing orange live-voice treatment for microphone/live-session
  state; it is a functional signal, not decoration.
- Use `--danger` only for errors, destructive actions, and active stop/recording
  states; `--success`, `--info`, and `--warn` carry the remaining status text.
- Never use gradients, glass effects, colored side-stripe cards, or decorative
  saturation.

## Color Tokens

The committed CSS custom properties are authoritative:

- Surface: `--bg`, `--bg-elevated`, `--bg-sidebar`
- Text: `--fg`, `--fg-muted`, `--sidebar-fg`, `--sidebar-muted`
- Structure: `--border`
- Action: `--accent`, `--accent-fg`, `--focus-ring`
- Conversation: `--user-bubble`, `--user-bubble-fg`,
  `--assistant-bubble`, `--assistant-bubble-fg`
- Semantic: `--danger`, `--success`, `--info`, `--warn`

Danger surfaces always pair `--danger` with `--danger-fg`; every theme must retain
at least 4.5:1 contrast for normal-size text.

Status text uses `--success` (completed, healthy, saved), `--info` (in progress),
and `--warn` (degraded, needs attention). Never write a literal hex for a
foreground color. A literal is fixed at author time, so it is correct only in the
theme it was authored against: `#b91c1c` for an error message measures 2.67:1 on
the dark surface, and `#fff` on a `var(--accent)` fill measures 1.07:1 in the
high-contrast theme. Pair a `var(--accent)` background with `var(--accent-fg)`,
which `ThemeProvider.readableForeground` derives from the accent actually in use
so it also holds for a user-chosen accent.

Both rules are enforced: `globals.contrast.test.ts` checks the token values and
their hue separation, and `components/themeTokens.test.ts` scans the sources for
literal foreground colors.

Muted and placeholder text must retain at least 4.5:1 contrast. State must never
depend on color alone.

## Typography

- Use the existing system sans stack for all product UI.
- Base size follows `--font-scale`; controls inherit the body font.
- Use a compact fixed hierarchy: page title 1.25-1.35rem, section title
  1-1.1rem, body 1rem, supporting text 0.8-0.875rem.
- Use weight and spacing before increasing size.
- Avoid uppercase tracked labels as repeated scaffolding.
- Keep explanatory prose within 65-75 characters where layout permits.

## Layout

- The transcript and composer are the primary workspace.
- Desktop uses a collapsible conversation sidebar, flexible chat canvas, and a
  340-380px contextual inspector.
- The inspector has one navigation level and progressive disclosure; do not put
  cards inside cards.
- On narrow screens, the conversation sidebar and inspector become labelled
  drawers with focus management, Escape close, and focus return.
- At 200% text and short mobile heights, the sidebar keeps its header fixed and
  scrolls conversations plus every destination/account utility as one reachable
  vertical region.
- The composer remains visible and usable without being covered by panels or
  clipped menus.
- Use the existing 8px control radius and 12px surface radius as defaults.

## Components

### Conversation Inspector

Sections are Model, Instructions, Agent & Tools, Context, Memory, Usage, and Voice.
Each section owns its loading, empty, partial, unavailable, error, and saved states.
Server-returned effective values and governance metadata take precedence over local
preferences.
Each section owns independent loading, error, empty, partial, saving, and saved
feedback. Session changes discard stale responses before enabling mutation.
Unknown tool governance metadata is labelled unknown and unavailable; the client
never fabricates risk, approval, scope, ownership, or channel defaults.

### Help

Help triggers are focusable buttons associated through `aria-describedby`. Help
content explains meaning, effect or tradeoff, default, and availability. It opens
on focus or pointer intent, closes on Escape, and renders outside clipping
containers. Its first measured position and scrollable max-height stay inside the
effective viewport. A `title` attribute is never the only explanation.

### Navigation

The left rail owns conversations and true destinations. Docs, status, admin,
account, and sign-out live in one utility/account area. The top bar contains only
conversation identity and meaningful activity state.

### Composer

The composer provides one Attach action for server-advertised media, one live-voice
microphone, the text field, and Send/Stop. Upload and processing state appears
inline. Image generation is a chat/tool capability rather than primary navigation.
Upload routing follows the server-advertised ingest path. A document appears as
selected context only after association succeeds, and in-flight uploads remain
bound to the conversation that started them.

### Data and Governance

Risk, approval, scope, inherited/override state, ownership, freshness, and unknown
values use text plus iconography. Unknown cost or telemetry displays "Unknown" or
"Unavailable", never a fabricated zero.
Admin window/identity refreshes replace prior data with a polite live loading state;
an empty-state message is never shown while a request is still pending.

## Interaction and Motion

- Use 150-250ms ease-out transitions only for state changes such as drawer,
  disclosure, selection, and saved feedback.
- Do not animate page load or layout dimensions unnecessarily.
- Under `prefers-reduced-motion: reduce`, transitions become effectively instant.
- Every control has default, hover, focus, active, disabled, loading, and error
  treatment where applicable.

## Accessibility

- WCAG 2.2 AA is the minimum; preserve the existing high-contrast mode.
- Maintain semantic landmarks, skip navigation, live regions, IME-safe input, and
  screen-reader labels.
- Drawers trap focus while open and restore it on close.
- Menus and tooltips must not be clipped by scroll containers.
- Verify keyboard-only operation, 200% zoom, reduced motion, and no horizontal
  page overflow at supported breakpoints.
