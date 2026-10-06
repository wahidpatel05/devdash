# devdash

A terminal dashboard (built with [Textual](https://textual.textualize.io)) for your
hackathon and dev projects. In one window you get git status for every project,
start/stop/restart for their dev servers, and live, searchable logs.

```
┌ Projects ──────────────┐┌ Services ───────────────────────────────────────────┐
│ ● hackathon-app    ▶ 2 ││ Service   Status     PID    Port  Listening  Uptime │
│ ● discord-bot          ││ frontend  ● running  4121   5173  ● up       3m 12s │
│ ✗ old-thing            ││ api       ■ exited 1 —      8000  ○ down     —      │
└────────────────────────┘└─────────────────────────────────────────────────────┘
┌ Git ───────────────────┐┌ Logs · api ─────────────────────────────────────────┐
│ Branch    main         ││ ── starting: uv run uvicorn app.main:app --reload   │
│ Remote    origin/main  ││ INFO:     Will watch for changes in these dirs      │
│           ↑1 ↓0        ││▏ModuleNotFoundError: No module named 'fastapi'      │
│ Changes   2 modified   ││ ── exited (exit code 1)                             │
└────────────────────────┘└───────────────────────────────── /error  match 1/1 ─┘
 q Quit  s Start  x Stop  r Restart  / Search  c Clear log  g Git refresh  f Fetch
```

## Install

Requires Python 3.11+ and git.

```sh
uv tool install git+https://github.com/wahidpatel05/devdash   # puts `devdash` in ~/.local/bin
devdash --init-config                                         # writes ~/.config/devdash/config.toml
$EDITOR ~/.config/devdash/config.toml
devdash
```

To hack on it:

```sh
git clone https://github.com/wahidpatel05/devdash && cd devdash
uv tool install --editable .   # `devdash` now runs your working copy
uv run pytest
```

## Config

`~/.config/devdash/config.toml` (override with `-c PATH` or `$DEVDASH_CONFIG`):

```toml
refresh_interval = 3          # seconds between git refreshes
theme = "omarchy"             # or any Textual theme: "tokyo-night", "nord", ...
log_lines = 5000              # output kept per service
scan_dirs = ["~/Projects"]    # auto-add every git repo directly inside these

[[projects]]                  # explicit entries override a scanned repo at the same path
name = "hackathon-app"
path = "~/Projects/hackathon-app"

  [[projects.services]]
  name = "frontend"
  command = "npm run dev"     # run with bash -c, so pipes, && and env vars work
  port = 5173                 # optional: devdash checks whether it's listening
  cwd = "web"                 # optional, relative to the project path
```

See [`src/devdash/example_config.toml`](src/devdash/example_config.toml) for a full
example.

**Auto-discovery (`scan_dirs`):** every git repo (including worktrees) directly
inside a scan dir becomes a project. Services are detected from a `dev` script in
`package.json`:
- If the repo root has one, that becomes a single service.
- Otherwise each subfolder (`frontend/`, `backend/`, `apps/*`, `packages/*`) with
  its own `dev` script becomes a service.

The right runner is picked from the lockfile (`npm`, `pnpm`, `yarn` or `bun`).
Ports are guessed from `--port` flags, the tool (`vite` → 5173, `next` → 3000, …),
or `PORT || 5000` in a Node entry file. Scanning happens at startup and whenever you press
`R`, which also re-reads config.toml. Services that are running when their
project disappears keep running, and stay listed until you stop them. For anything detection misses (Python
backends, non-git folders), add an explicit `[[projects]]` entry for that path.

Error handling:
- An invalid project or service entry is skipped with a warning; everything else
  still loads.
- A missing config file or broken TOML shows an error screen instead of crashing.
- Errors in individual repos (missing path, not a repo, git failing) show up in the
  Git panel.

## Keys

| Key           | Action                                                 |
|---------------|--------------------------------------------------------|
| Tab           | Move focus: projects → services → logs                 |
| ↑ ↓ / j k     | Select project / service, or scroll logs               |
| s / x / r     | Start / stop / restart the selected service            |
| /             | Search the selected service's log (Enter to find)      |
| n / N         | Next / previous match                                  |
| Esc           | Close the search box, or clear the active search      |
| c             | Clear the selected service's log                       |
| G             | Jump to the end of the log (resumes following)         |
| g             | Refresh git status now                                 |
| R             | Rescan: re-read config.toml and `scan_dirs` without restarting |
| f             | `git fetch` the selected project                       |
| q             | Quit: stops every service devdash started (q again to force) |
| ctrl+p        | Command palette (switch theme, etc.)                   |

## How it behaves

- **Git status** comes from `git status --porcelain=v2` and `git log`, run
  asynchronously, so a slow repo never freezes the UI. Ahead/behind is measured
  against your *local* copy of the upstream branch, so press `f` when you want it
  fresh. devdash never fetches on its own.
- **Services** each run in their own process group. Stop sends SIGTERM to the
  whole group (so `npm → node → esbuild` all go down), waits 5s, then sends SIGKILL.
- **Cleanup:** quitting stops everything devdash started. So does closing the
  terminal window (SIGHUP). Services are launched under `setpriv --pdeathsig`, so
  they're terminated even if devdash itself is killed with `kill -9`. devdash never
  touches processes it didn't start.
- **Listening:** shows `● up` when the port accepts connections on localhost
  (IPv4 or IPv6), `○ waiting` while a started service hasn't bound yet, and
  `● in use (not ours)` when something else already holds the port.
- **Logs** keep the process's own ANSI colours. Lines containing *error*,
  *exception* or *failed* are highlighted, and stderr lines get a gutter mark.
  The view follows new output only while you're scrolled to the bottom.

## Theming

With `theme = "omarchy"` (the default), devdash builds its palette from
`~/.local/state/omarchy/current/theme/colors.toml`, and re-colours itself live
when you run `omarchy theme set …`. Off Omarchy it falls back to Textual's default
theme.

All colours used by the UI are defined as variables at the top of
[`src/devdash/ui/devdash.tcss`](src/devdash/ui/devdash.tcss). To override them
without editing the package, create `~/.config/devdash/devdash.tcss` (next to
your config). It's loaded after the built-in stylesheet and can use theme
variables such as `$accent`, `$success`, `$error`:

```css
#logs { background: $background-darken-1; }
LogView > .logview--error { color: #ff5555; text-style: bold; }
LogView > .logview--match { background: $warning; }
ServicesTable > .services--good { color: $success; }
```

If that file has a syntax error, devdash ignores it and tells you why, rather than
refusing to start.

## Hyprland keybind (Omarchy)

Add this to `~/.config/hypr/bindings.lua`:

```lua
-- devdash: open, or focus it if it's already open, as a floating window
o.bind("SUPER + ALT + D", "Dev dashboard", { tui = "devdash", focus = true })
o.window("^org\\.omarchy\\.devdash$", { float = true, center = true, size = { 1400, 860 } })
```

Then validate it with `hyprctl reload && hyprctl configerrors`.

- `{ tui = …, focus = true }` runs it through `omarchy-launch-or-focus-tui`. That
  opens devdash in your default terminal with the app-id `org.omarchy.devdash`,
  or focuses the existing window, so you never end up with two instances fighting
  over ports.
- Adjust `size` to taste (in pixels). `SUPER + SHIFT + D` is already taken by
  Docker in Omarchy, which is why this uses `SUPER + ALT + D`.

## Layout

```
src/devdash/
  cli.py               argument parsing, entry point, last-resort cleanup
  config.py            TOML loading + validation
  git.py               async git queries (never block, never raise)
  services.py          process manager: start/stop/restart, log capture, port checks
  theme.py             Omarchy colors.toml → Textual theme
  ui/app.py            the App: layout, keybindings, timers, wiring
  ui/widgets.py        project list + git panel
  ui/services_table.py services table
  ui/logview.py        log view with highlighting and search
  ui/devdash.tcss      all styling
tests/                 config, git and process-manager tests
```
