# Project guide

Dell Warranty Sync for Freshservice — a Python + pywebview desktop app that
compares Freshservice asset warranty dates against Dell TechDirect and writes
back the differences. UI lives in `web/`; decoupled backend classes live in
`app/` (Dell, Freshservice and sync-engine classes are reusable from the Admin
Suite without the UI).

## Design

**All UI and visual-design work in this project follows [DESIGN.md](./DESIGN.md)
unless the request explicitly says otherwise.** Reuse its colour tokens,
typography (IBM Plex Sans/Mono), components (sidebar, cards, buttons, toggles,
status badges, table, toast, progress, terminal log) and the status-colour
vocabulary rather than introducing new styles. Keep the light theme; if dark
mode is needed, extend the tokens in DESIGN.md rather than hard-coding colours.

This is the house style for the Admin Suite — prefer it for new internal tools
too, unless told otherwise.

## Conventions

- Keep the backend UI-free and decoupled (`app/dell.py`, `app/freshservice.py`,
  `app/sync.py`); the bridge (`app/bridge.py`) is the only JS↔Python seam.
- Secrets go to the OS credential store via `keyring` — never to disk.
- `demo_mode` (on by default) must keep working with no network calls.
