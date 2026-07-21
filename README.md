# Ollama Model Manager

A single-file, zero-dependencies curses TUI for browsing, installing, and
uninstalling [Ollama](https://ollama.com) models — wraps `ollama pull` and `ollama rm` commands for you.

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

The TUI shows installed models as well as all models
*available* for installation. You can batch-select several models to install/uninstall 
and then `Apply` will run the corresponding `ollama pull` / `ollama rm` commands.

## Requirements

- Python 3.8+ (uses only the standard library: `curses`, `urllib`,
  `subprocess`, `threading`, `queue`, `webbrowser`, `json`, `re`)
- `ollama` CLI on your `PATH` (used for `pull`/`rm` operations)
- A running local Ollama daemon at `http://localhost:11434` (used to
  *list* what's currently installed — the app runs without it, just
  showing zero installed models)
- A terminal with `curses` support (Linux/macOS natively; on Windows, run
   under WSL or install `windows-curses`)
- Internet access, *optional* — needed to populate the
  **Available Models** catalog. Everything else works fully offline.

## Usage

```bash
python3 ollama-model-manager.py
```

No install step, no virtualenv, no pip requirements file — it's a single standalone file.

## UI: what each branch means

```
Ollama Models
├── Local Models       registered on this machine, real weight blobs on disk,
│                      works fully offline
├── Cloud Models       registered on this machine (shows up in `ollama ls`),
│                      tagged with `:cloud` (or `:<size>-cloud`) — this is a
│                      shallow Modelfile/manifest pull with NO weight blobs.
│                      Inference for these runs on Ollama's servers,
│                      not on your machine. Requires internet.
└── Available Models   the remote catalog (ollama.com) of
                       everything you can pull — both
                       local-weight models and remote `:cloud` models
```

## Keyboard keys

| Key(s)             | Context           | Action                                              |
|---------------------|-------------------|------------------------------------------------------|
| `↑` / `↓`           | Model tree        | Move selection                                        |
| `→`                 | Model tree        | Expand a collapsed group, or jump into its first child|
| `←`                 | Model tree        | Collapse an expanded group, or jump to the parent row |
| `Space` or `Enter`  | Model tree (leaf) | Toggle the checkbox (see "Picking a tag variant" below)|
| `Enter`             | Model tree (group)| Expand/collapse (same as `→`/`←`)                     |
| `i`                 | Model tree (leaf) | Open the model's `ollama.com` page in your browser    |
| `Tab` / `Shift+Tab` | Anywhere          | Cycle focus: Model Panel → Apply → Quit → Model Panel |
| `Space` / `Enter`   | Apply button      | Compute the diff and open the confirmation dialog     |
| `Space` / `Enter`   | Quit button       | Quit                                                   |
| `q` / `Q`           | Anywhere          | Quit immediately (blocked with a message mid-Apply)   |
| `←`/`→`/`Tab`       | Confirm dialog    | Switch between Confirm / Cancel                        |
| `Enter`             | Confirm dialog    | Activate the highlighted button                        |
| `y` / `n` / `Esc`   | Confirm dialog    | Confirm / Cancel / Cancel                              |
| `↑` / `↓`           | Tag picker        | Move between variants                                  |
| `Enter`             | Tag picker        | Select the highlighted variant                          |
| `Esc` / `n`         | Tag picker        | Cancel (leaves the model unchecked)     

## Picking a tag variant

For certain models multiple tags are available — different sizes,
quantizations, or hardware-specific builds (MLX, etc):

1. The app fetches `https://ollama.com/library/<model>/tags`.
2. If there's more than one variant, a picker opens showing
   the exact string `ollama pull` needs and its
   download size, e.g.:

   ```
   Select a variant of 'qwen3.6' to install:
   > qwen3.6:latest        (24GB)
     qwen3.6:27b            (17GB)
     qwen3.6:27b-coding-mxfp8  (31GB)
     qwen3.6:35b-a3b-mtp-bf16  (72GB)
   ```

   `↑`/`↓` to move, `Enter` to pick, `Esc`/`n` to cancel without checking
   anything.

## The Apply workflow

1. Check/uncheck whatever you want installed or removed, anywhere in the tree.
2. Focus **Apply** (`Tab`) and activate it (`Space`/`Enter`).
3. The app computes `checked − installed` (installs) and
   `installed − checked` (uninstalls) by canonical name.
   - If both are empty: nothing happens, and the Log panel says so —
     Apply never silently no-ops without telling you.
   - Otherwise: a modal lists exactly what will be pulled and what will
     be removed. **Nothing runs until you confirm.**
4. On confirm, installs run first, then removals, each as a
   `ollama pull <name>` / `ollama rm <name>` subprocess with full
   command line and output streamed live into the Log
   panel.


