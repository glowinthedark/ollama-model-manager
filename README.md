# Ollama Model Manager

A single-file, dependency-free curses TUI for browsing, installing, and
removing [Ollama](https://ollama.com) models — without leaving the terminal
or memorizing `ollama pull` / `ollama rm` invocations.

```
┌ OLLAMA MODEL MANAGER ──────────────────────────────────────────────┐
│ ▼ Ollama Models                                                    │
│   ▼ Local Models                                                   │
│     [x] llama3.2:latest                             [Installed]    │
│     [x] mirage335/Qwen3-Coder-30b-virtuoso:latest   [Installed]    │
│   ▼ Cloud Models                                                   │
│     [x] deepseek-v4-flash:cloud                     [Installed]    │
│   ▶ Available Models                                               │
│ ───────────────────────────────────────────────────────────────────│
│  Logs                                                              │
│  Connecting to local Ollama daemon (http://localhost:11434/...)    │
│  Local daemon reachable. Found 3 installed model(s).               │
│  Retrieved 4370 model(s) from ollamadb.dev.                        │
│  Tab/Shift+Tab: switch panel | Space/Enter: toggle | i: model info │
│ ───────────────────────────────────────────────────────────────────│
│                  [ Apply ]              [ Quit ]                   │
└────────────────────────────────────────────────────────────────────┘
```

## Why this exists

`ollama pull` and `ollama rm` are fine one at a time, but there's no
built-in way to: see everything installed alongside everything
*available*, batch-select several models, or double-check exactly what's
about to change before it happens. This tool is that missing layer — a
checkbox tree over your local install state and the remote catalog, with
one `Apply` action that diffs the two and asks for confirmation.

## Requirements

- Python 3.8+ (uses only the standard library: `curses`, `urllib`,
  `subprocess`, `threading`, `queue`, `webbrowser`, `json`, `re`)
- `ollama` CLI on your `PATH` (used for the actual `pull`/`rm` operations)
- A running local Ollama daemon at `http://localhost:11434` (used only to
  *list* what's currently installed — the app still runs without it, just
  showing zero installed models)
- A terminal with `curses` support (Linux/macOS natively; on Windows, run
  it under WSL or install `windows-curses`)
- Outbound internet access, *optional* — only needed to populate the
  **Available Models** catalog. Everything else works fully offline.

## Running it

```bash
python3 ollama-model-manager.py
```

No install step, no virtualenv, no pip requirements file — it's one file.

## The tree, and what each branch actually means

This is the part worth reading carefully, because "cloud" is an
overloaded word in the Ollama ecosystem and this tool deliberately picks
one precise meaning for it and uses it consistently everywhere.

```
Ollama Models
├── Local Models       registered on this machine, real weight blobs on disk,
│                      works fully offline
├── Cloud Models       registered on this machine (shows up in `ollama ls`),
│                      but tagged `:cloud` (or `:<size>-cloud`) — this is a
│                      shallow Modelfile/manifest pull with NO weight blobs.
│                      Inference for these actually runs on Ollama's own
│                      infrastructure, not on your machine. Requires network
│                      access at inference time even though it's "installed".
└── Available Models   the remote catalog (ollamadb.dev / ollama.com) of
                       everything you could still pull — a mix of ordinary
                       local-weight models and remote `:cloud` models,
                       not yet registered on this machine
```

**Why the split matters:** grouping "Local Models" and "Cloud Models"
together under one generic "installed" label would silently misrepresent
disk usage, offline-readiness, and network dependency for the `:cloud`
entries. This tool treats "installed/registered" (state) and "cloud vs.
local" (execution locality) as two independent axes, and the tree reflects
both without conflating them.

A model can appear **twice** — once under Local/Cloud Models (if
registered) and once under Available Models (the catalog is not aware of
your local state). The `[Installed ...]` badge and the pre-checked
checkbox keep those two views in sync (see below).

### Checkbox identity: canonical names

Checkboxes are keyed by *canonical name* — the model name with any
`:tag` suffix stripped, but namespaces (e.g. `mirage335/`) always kept.
This means checking the bare `Available Models` entry for a model that's
already registered under a specific tag (e.g. `llama3.2:latest`) is
recognized as "no change," and unchecking a `Local Models` entry with a
non-default tag correctly targets that exact registered install for
removal.

### The `[Installed ...]` badge

- Under **Local Models** / **Cloud Models**, the display name already
  contains the exact registered tag, so the badge just reads `[Installed]`.
- Under **Available Models**, entries are bare/untagged (that's how the
  catalog lists them), so if a tagged variant is registered, the badge
  spells it out — e.g. `deepseek-v4-flash` shows `[Installed :cloud]`,
  making it clear you have the cloud-proxy variant, not a real local
  download. If multiple tags of the same model are registered, all of
  them are listed, comma-separated.

## Keybindings

| Key(s)             | Context           | Action                                              |
|---------------------|-------------------|------------------------------------------------------|
| `↑` / `↓`           | Model tree        | Move selection                                        |
| `→`                 | Model tree        | Expand a collapsed group, or jump into its first child|
| `←`                 | Model tree        | Collapse an expanded group, or jump to the parent row |
| `Space` or `Enter`  | Model tree (leaf) | Toggle the checkbox                                   |
| `Enter`             | Model tree (group)| Expand/collapse (same as `→`/`←`)                     |
| `i`                 | Model tree (leaf) | Open the model's `ollama.com` page in your browser    |
| `Tab` / `Shift+Tab` | Anywhere          | Cycle focus: Model Panel → Apply → Quit → Model Panel |
| `Space` / `Enter`   | Apply button      | Compute the diff and open the confirmation dialog     |
| `Space` / `Enter`   | Quit button       | Quit                                                   |
| `q` / `Q`           | Anywhere          | Quit immediately (blocked with a message mid-Apply)   |
| `←`/`→`/`Tab`       | Confirm dialog    | Switch between Confirm / Cancel                        |
| `Enter`             | Confirm dialog    | Activate the highlighted button                        |
| `y` / `n` / `Esc`   | Confirm dialog    | Confirm / Cancel / Cancel                              |

## The Apply workflow

1. Check/uncheck whatever you want installed or removed, anywhere in the
   tree.
2. Focus **Apply** (`Tab`) and activate it (`Space`/`Enter`).
3. The app computes `checked − installed` (installs) and
   `installed − checked` (uninstalls) by canonical name.
   - If both are empty: nothing happens, and the Log panel says so —
     Apply never silently no-ops without telling you.
   - Otherwise: a modal lists exactly what will be pulled and what will
     be removed. **Nothing runs until you confirm.**
4. On confirm, installs run first, then removals, each as a real
   `ollama pull <name>` / `ollama rm <name>` subprocess with its exact
   command line and every line of output streamed live into the Log
   panel.
5. When finished, the local model list is re-fetched and the Local/Cloud
   Models branches and checkbox state are rebuilt from the *actual*
   post-apply state (not just assumed from what you clicked).

## Data sources and fallback chain

**Local installed models** — `GET http://localhost:11434/api/tags`
(Ollama's native REST API). If the daemon isn't reachable, the app logs
the error and continues with zero installed models rather than crashing.

**Available Models catalog** — three-tier fallback, in order:

1. **`https://ollamadb.dev/api/v1/models`** — a validated, live,
   third-party community API ([frefrik/ollama-models-api](https://github.com/frefrik/ollama-models-api),
   MIT-licensed, *not officially affiliated with Ollama*). Paginated
   automatically; namespace and model name are reconstructed from the
   documented `namespace`/`model_name` fields rather than trusting a
   single flat field, so community/namespaced models keep their full
   `namespace/model` prefix. The API's own per-model `url` field is
   captured too, used by the `i` (model info) keybind when available.
2. **Scrape of `https://ollama.com/library`** — used only if (1) fails
   or returns nothing. Limitation: this page only lists official,
   non-namespaced models; community/namespaced models are invisible via
   this path.
3. **Small hardcoded list** — last-resort safety net so the UI is never
   completely empty even with no network at all.

Every step of this chain — which source was tried, how many results it
returned, and any failure — is echoed to the Log panel as it happens.

## Design notes / known limitations

- **Available Models can't yet distinguish, per-entry, which specific
  tags are local-weight vs. `:cloud`.** The catalog API returns a tag
  *count* per model family, not the enumerated tag list, so knowing in
  advance that (say) `qwen3:235b-cloud` exists as a pullable tag would
  require a further per-model lookup (e.g. `ollama.com`'s model page or
  `/api/show`). This is only a gap for models you *don't* have installed
  yet; once something is registered, its exact tag is always shown
  correctly under Local/Cloud Models.
- **Checkbox identity is per canonical name, not per exact tag.** If you
  have both `llama3:8b` and `llama3:70b` registered, unchecking either
  will target that specific exact install for removal (each is its own
  row), but the Available Models entry `llama3` reflects "installed" as
  soon as *any* tag of it is present.
- **Redraws are event-driven, not timer-driven**, to avoid the flicker
  that comes from unconditionally clearing and repainting the whole
  screen every tick. The screen only repaints on a keypress, new log
  output, or while a pull/remove is actively streaming output.
- Requires the `ollama` binary to exist on `PATH` for Apply to do
  anything; if it's missing, each queued command logs a clear error
  instead of crashing the process.

## File layout

Everything lives in `ollama-model-manager.py` — no package, no build
step, no config file. It's meant to be read top-to-bottom in one sitting;
the module docstring at the top of the file summarizes every design
decision with a one-line pointer back into this README's explanations.
