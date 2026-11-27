# Design system

The visual language used by this app (and the house style for the Admin Suite).
A clean, dense **enterprise admin tool**: calm greys, a confident blue accent,
monospaced data, generous rounding and restrained motion. Apply these tokens and
components to new UI unless a request says otherwise.

## Principles

- **Data-first and legible.** Monospace for anything machine-ish (IDs, dates,
  tags, field names, log output); proportional for prose and labels.
- **One accent.** Blue carries primary actions and active state. Status is the
  only other colour, and always reads by lightness as well as hue.
- **Quiet surfaces.** White cards on a soft grey page, hairline borders, soft
  rounding. Depth comes from borders and spacing, not heavy shadow.
- **Calm motion.** Short, functional transitions only (spinners, a progress
  shimmer, a one-shot row fade). Nothing decorative.
- **Accessible by construction.** Real `<button>`/`<a>`/`<input>`; icon-only
  controls get `aria-label`; text meets 4.5:1 (small print 3:1 at ≥24px).

## Colour tokens

Defined on `:root` in `web/styles.css`. Light theme only today — if dark mode is
needed, redefine these under `@media (prefers-color-scheme: dark)` and keep the
same token names.

| Token | Value | Role |
| --- | --- | --- |
| `--brand-dark` | `#003087` | Title bar, sidebar, app chrome |
| `--brand-blue` | `#005EB8` | Primary actions, active state, focus ring |
| `--brand-blue-hover` | `#004a92` | Primary hover |
| `--brand-bright` | `#0072CE` | Progress shimmer, info toast dot |
| `--page` | `#f0f4f5` | Main content background |
| `--card` | `#ffffff` | Card / panel surface |
| `--card-border` | `#e1e7ea` | Card / table hairline border |
| `--ink` | `#212b32` | Primary text |
| `--muted` | `#4c6272` | Secondary text, labels |
| `--faint` | `#768692` | Tertiary text, table headers |
| `--faint-2` / `--faint-3` | `#8a97a0` / `#8a99a1` | Disabled / placeholder |
| `--input-bg` | `#f7f9fa` | Input fields |
| `--input-border` | `#ccd6db` | Input border |
| `--green` / `--green-dot` / `--green-live` | `#00703c` / `#009b48` / `#45d17f` | Success text / dot / live indicator |
| `--terminal` | `#0b1f33` | Log / terminal surface |

### Status vocabulary (badges, pills)

Each status is a tint background + readable text + a solid dot. Differ in
lightness, not hue alone.

| Status | bg | text | dot |
| --- | --- | --- | --- |
| update (pending) | `#fff3d6` | `#8a5a00` | `#e0a000` |
| match (in sync) | `#e3f1ea` | `#00703c` | `#009b48` |
| synced (just written) | `#cdeadd` | `#005c31` | `#007f3b` |
| no-data / no-tag | `#eaeef0` | `#4c6272` | `#8a99a1` |
| invalid / warning | `#ffe7cc` | `#8a4b00` | `#ed8b00` |
| error | `#f9dedb` | `#8f1a10` | `#d5281b` |

Day-count / delta colours: overdue `#c62f22`, soon (<90d) `#c47f00`, normal
`--muted`.

## Typography

- **Sans:** `'IBM Plex Sans', system-ui, sans-serif` (weights 400/500/600/700).
- **Mono:** `'IBM Plex Mono', monospace` (400/500/600) — IDs, tags, dates, field
  names, URLs, log lines. Load both from Google Fonts.
- **Scale:** page title `23px/600` (letter-spacing -.2px); section/card title
  `15px/600`; sub-head `13.5px`; body `13.5px`; label `11.5–12px/500–600`;
  table header `10.5px/600` uppercase, letter-spacing `.7px`, colour `--faint`;
  big stat `27px/600`. Antialiased (`-webkit-font-smoothing: antialiased`).

## Shape, spacing, depth

- **Radius:** cards/panels `12px`; buttons/inputs/selects `8px`; icon tiles
  `8–9px`; pills, badges, toggles, progress track `999px`.
- **Spacing:** screen padding `26px 32px 40px`; card padding `16–22px`; control
  gaps `10–18px`; sidebar width `244px` (collapses to `68px` icon rail ≤880px).
- **Borders over shadows:** `1px` hairlines (`--card-border`). Shadows only for
  lifted/floating elements — toast `0 12px 34px rgba(0,0,0,.28)`, toggle knob
  `0 1px 2px rgba(0,0,0,.28)`.

## Components

- **App chrome:** frameless window. Dark-blue (`--brand-dark`) 40px title bar
  with left brand + right window controls (minimise / maximise / close; close
  hovers red `#d5281b`). Dark-blue 244px **sidebar**: brand block, uppercase
  section heading, nav items (icon + label, left 3px accent bar + tinted bg when
  active, count pill on the right), and a footer with connection dots + version.
- **Buttons:** primary (solid `--brand-blue`, white, 600); ghost (white, 1px
  border, hovers to `--brand-blue`); outline (white, blue border + text);
  success (solid `--green`). Padding ~`10–11px 16–20px`, radius 8px, icon +
  label with `gap` 8px.
- **Toggle switch:** 38×22 track, 18px knob; off `#c2ccd2`, on `--brand-blue`.
- **Pills / badges:** rounded-full, tint bg + dot + label per status table.
- **Cards:** white, 12px radius, hairline border. Summary/stat cards show a
  dot + label + large number, selectable (2px border turns to the stat's tone).
- **Table:** white card, uppercase hairline header row (`#f6f9fa`), 11px cell
  padding, row hover `#f5f9fc`, mono for IDs/tags/dates, numeric columns right-
  aligned, status badge in the last column. Horizontal scroll when narrow.
- **Inputs / selects:** `--input-bg`, 1px `--input-border`, 8px radius; focus →
  `--brand-blue` border, no outline. Mono for machine values.
- **Toast:** centred bottom, dark `--ink` pill, dot + message, slides up + fades
  (`opacity/transform .25s`), auto-dismiss ~3.4s.
- **Progress:** 8px rounded track; fill is a `repeating-linear-gradient` of
  `--brand-bright`/`--brand-blue` with a `barflow` shimmer; mono percentage.
- **Log / terminal:** `--terminal` surface, IBM Plex Mono `12.5px`, line colours
  by severity (green success, blue info, amber warn, red error, grey default).
- **Empty state:** centred tinted icon tile + title + one-line explanation +
  primary action.

## Iconography

Inline SVG, `viewBox="0 0 24 24"`, `fill="none"`, `stroke="currentColor"`,
`stroke-width` ~1.7–2, round caps/joins (Feather-style line icons). Decorative
SVGs `aria-hidden`; meaningful ones get `role="img"` + `aria-label`.

## Motion

- `spin .8s linear infinite` (loading), `barflow .7s linear infinite` (progress
  shimmer), `rowin .25s ease` (one-shot row entrance).
- Interactive transitions `.12–.16s` on background/border/colour/transform.

## Layout & responsiveness

Fills the window (content max-width `1500px`, left-aligned dashboard style).
Breakpoints: cards 6→3→2 columns; config forms collapse to one column ≤1024px;
sidebar collapses to an icon rail ≤880px; control row stacks ≤560px. Works down
to phone width with a 16px gutter and no horizontal page scroll.

## Implementation notes

- Reference tokens as CSS variables; never hard-code a hex that has a token.
- Keep machine values in mono and prose in sans — it's the core of the look.
- New status types extend the status table; don't invent unrelated colours.
- This is a native-feeling desktop UI (pywebview). For responsive web/app work,
  the same tokens apply but revisit layout density.
