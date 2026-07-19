#!/usr/bin/env python3
#
# Copyright (C) 2026 glowinthedark <glwnd2030@gmail.com>
# SPDX-License-Identifier: GPL-3.0-or-later
#
# Ollama Model Manager - a curses TUI for browsing, installing and removing Ollama models.
#

import curses
import urllib.request
import urllib.parse
import json
import subprocess
import threading
import queue
import re
import webbrowser

LOCAL_API_URL = "http://localhost:11434/api/tags"
CLOUD_API_URL = "https://ollamadb.dev/api/v1/models" ## (temporarily[?] down)
LIBRARY_FALLBACK_URL = "https://ollama.com/library"

HARDCODED_FALLBACK = [
    "llama3.2", "phi3", "mistral", "gemma2", "qwen2.5", "mixtral", "llava",
    "deepseek-r1", "qwen3", "gpt-oss",
]


def canonical(name):
    """Base identity of a model name, ignoring the :tag suffix.
    Namespace (e.g. 'mirage335/foo') is NEVER stripped -- only the part
    after the first ':' is dropped for comparison purposes. Display strings
    always keep the original, untouched name."""
    return name.split(":", 1)[0]


def is_cloud_tag(name):
    """True if this exact registered model name is one of Ollama's remote
    ':cloud' proxy installs -- a shallow Modelfile/manifest pull with no
    weight blobs, where inference actually runs on Ollama's infrastructure
    rather than on this machine. Covers both the plain ':cloud' tag and the
    size-qualified convention (e.g. 'qwen3:235b-cloud')."""
    if ":" not in name:
        return False
    tag = name.split(":", 1)[1]
    return tag == "cloud" or tag.endswith("-cloud")


class ConfirmDialog:
    """Modal confirmation state for a pending Apply action."""
    def __init__(self, to_install, to_uninstall):
        self.to_install = to_install
        self.to_uninstall = to_uninstall
        self.selected = 0  # 0 = Confirm, 1 = Cancel


class TagPickerDialog:
    """Modal variant-picker shown when the user checks a bare (untagged)
    Available Models entry that has more than one pullable tag -- e.g.
    'qwen3.6' resolves to qwen3.6:latest, qwen3.6:27b, qwen3.6:35b-a3b, etc.
    Only the exact pull string and its download size are shown, per spec:
    everything else on the ollama.com tags page (context window, modality,
    digest, relative date) is noise for a "what do I pull" decision."""
    def __init__(self, base, options):
        self.base = base            # canonical model name these tags belong to
        self.options = options      # list of (pull_name, size_str), sorted
        self.selected = 0


class OllamaTUI:
    def __init__(self, stdscr):
        self.stdscr = stdscr
        curses.curs_set(0)
        curses.start_color()          # was missing: use_default_colors()/init_pair()
        curses.use_default_colors()   # require start_color() to have been called first

        curses.init_pair(1, curses.COLOR_CYAN, -1)
        curses.init_pair(2, curses.COLOR_GREEN, -1)
        curses.init_pair(3, curses.COLOR_BLACK, curses.COLOR_WHITE)
        curses.init_pair(4, curses.COLOR_YELLOW, -1)
        curses.init_pair(5, curses.COLOR_RED, -1)
        curses.init_pair(6, curses.COLOR_MAGENTA, -1)

        self.log_queue = queue.Queue()
        self.logs = []
        self.is_processing = False
        self.state_lock = threading.Lock()
        self.cloud_urls = {}  # canonical(name) -> ollama.com URL, populated during fetch

        self.draw_loading("Initializing Ollama Model Manager...")

        self.installed_models = self.fetch_local_models()
        self.cloud_models = self.fetch_cloud_models()  # list of full display names

        self.tree = [
            {
                "name": "Ollama Models",
                "is_group": True,
                "expanded": True,
                "children": [
                    {
                        "name": "Local Models",
                        "is_group": True,
                        "expanded": True,
                        "children": [
                            {"name": m, "is_group": False}
                            for m in self.installed_models if not is_cloud_tag(m)
                        ],
                    },
                    {
                        "name": "Cloud Models",
                        "is_group": True,
                        "expanded": True,
                        "children": [
                            {"name": m, "is_group": False}
                            for m in self.installed_models if is_cloud_tag(m)
                        ],
                    },
                    {
                        "name": "Available Models",
                        "is_group": True,
                        "expanded": False,
                        "children": [{"name": m, "is_group": False} for m in self.cloud_models],
                    },
                ],
            }
        ]

        # Requirement 1: installed models are pre-checked.
        self.checked = {canonical(m) for m in self.installed_models}

        self.visible_lines = []
        self.update_visible_lines()

        self.selected_row = 0
        self.tree_offset_y = 0
        self.focus = "tree"          # 'tree' | 'apply' | 'quit'
        self.dialog = None           # ConfirmDialog or None
        self.tag_picker = None       # TagPickerDialog or None
        self.is_fetching_tags = False
        self.selected_tag_for_install = {}  # canonical(base) -> exact pull string
                                             # chosen via the tag picker, overrides
                                             # the default bare-name (:latest) pull

        self.needs_redraw = True     # anti-flicker: only paint on real changes

    # ------------------------------------------------------------------ #
    # Data fetching
    # ------------------------------------------------------------------ #
    def log(self, msg):
        self.log_queue.put(msg)

    def draw_loading(self, msg):
        self.stdscr.erase()
        h, w = self.stdscr.getmaxyx()
        y, x = h // 2, max(0, (w - len(msg)) // 2)
        try:
            self.stdscr.addstr(y, x, msg, curses.color_pair(1) | curses.A_BOLD)
        except curses.error:
            pass
        self.stdscr.refresh()

    def fetch_local_models(self):
        self.log(f"Connecting to local Ollama daemon ({LOCAL_API_URL}) ...")
        try:
            req = urllib.request.Request(LOCAL_API_URL)
            with urllib.request.urlopen(req, timeout=3) as response:
                data = json.loads(response.read().decode())
                models = sorted(m["name"] for m in data.get("models", []))
                self.log(f"Local daemon reachable. Found {len(models)} installed model(s).")
                return models
        except urllib.error.URLError as e:
            self.log(f"ERROR: Could not reach local Ollama daemon: {e}")
            self.log("Is 'ollama serve' running? Proceeding with 0 installed models.")
            return []
        except Exception as e:
            self.log(f"ERROR: Unexpected failure reading local models: {e}")
            return []

    def fetch_cloud_models(self):
        """Returns a flat, sorted list of *full* model names (namespace and
        tag preserved verbatim, never stripped) available for download."""
        models = []

        # Attempt 1: ollamadb.dev community API (validated live endpoint).
        # this is a DEAD parrot!
        # self.log(f"Querying community model catalog ({CLOUD_API_URL}) ...")
        # try:
        #     models = self._fetch_from_ollamadb()
        #     if models:
        #         self.log(f"Retrieved {len(models)} model(s) from ollamadb.dev.")
        # except Exception as e:
        #     self.log(f"WARNING: ollamadb.dev query failed: {e}")

        # Attempt 2: scrape the official library index as fallback.
        if not models:
            self.log(f"Falling back to scraping {LIBRARY_FALLBACK_URL} ...")
            try:
                models = self._fetch_from_library_scrape()
                if models:
                    self.log(f"Parsed {len(models)} model(s) from ollama.com/library "
                              f"(namespaced/community models are not visible via this path).")
            except Exception as e:
                self.log(f"WARNING: Library scrape failed: {e}")

        # Attempt 3: hardcoded safety net so the UI is never empty.
        if not models:
            self.log("All remote sources unavailable. Using built-in fallback list.")
            models = list(HARDCODED_FALLBACK)

        return sorted(set(models))

    def _fetch_from_ollamadb(self):
        """Paginates through GET /models, preserving namespace prefixes.

        Documented schema (frefrik/ollama-models-api):
          model_identifier, namespace, model_name, model_type, pulls, ...
        namespace is null for official models and set for community models
        (e.g. namespace='mirage335', model_name='Qwen3-Coder-30b-virtuoso').
        We reconstruct 'namespace/model_name' ourselves rather than trusting
        model_identifier's exact format, since that field is documented as a
        filter key, not guaranteed to be the canonical display slug.
        """
        results = []
        skip = 0
        limit = 200
        total_count = None
        while True:
            qs = urllib.parse.urlencode({"limit": limit, "skip": skip})
            req = urllib.request.Request(f"{CLOUD_API_URL}?{qs}",
                                          headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=5) as response:
                data = json.loads(response.read().decode())

            batch = data.get("models", [])
            if not batch:
                break

            for m in batch:
                namespace = m.get("namespace")
                model_name = m.get("model_name") or m.get("model_identifier")
                if not model_name:
                    continue
                full_name = f"{namespace}/{model_name}" if namespace else model_name
                results.append(full_name)
                url = m.get("url")
                if url:
                    self.cloud_urls[canonical(full_name)] = url

            total_count = data.get("total_count", len(results))
            skip += limit
            if skip >= total_count or len(batch) < limit:
                break
            # Safety cap so a misbehaving API can't hang the app forever.
            if skip > 5000:
                self.log("WARNING: Stopping pagination after 5000 entries (safety cap).")
                break

        return results

    def _fetch_from_library_scrape(self):
        req = urllib.request.Request(LIBRARY_FALLBACK_URL,
                                      headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=5) as response:
            html = response.read().decode()
        matches = re.findall(r'href="/library/([^/"?#]+)"', html)
        return list(dict.fromkeys(m for m in matches if m))

    # ------------------------------------------------------------------ #
    # Tree helpers
    # ------------------------------------------------------------------ #
    def update_visible_lines(self):
        self.visible_lines = []

        def flatten(nodes, depth):
            for node in nodes:
                self.visible_lines.append((node, depth))
                if node.get("is_group") and node["expanded"]:
                    flatten(node["children"], depth + 1)

        flatten(self.tree, 0)

    def installed_variants(self, leaf_name):
        """All exact installed model strings sharing this leaf's canonical
        identity. Used to surface tag-specific installs (e.g. a Cloud Models
        entry 'deepseek-v4-flash' whose only installed form is actually
        'deepseek-v4-flash:cloud') that a bare canonical match would hide."""
        target = canonical(leaf_name)
        return [m for m in self.installed_models if canonical(m) == target]

    def fetch_model_tags(self, base):
        """Live-fetch the pullable tag variants for one model from its
        ollama.com library page, e.g. https://ollama.com/library/qwen3.6/tags.
        Returns a sorted list of (pull_name, size_str) -- only the exact
        string to hand to `ollama pull` and its download size, since that's
        what actually matters for picking a variant (context window,
        modality, and digest shown on that page are not).

        Best-effort HTML scrape against an undocumented page (there is no
        JSON API for per-model tag enumeration as of this writing) -- kept
        deliberately tolerant of markup drift: it locates tag links by their
        href alone, then searches the *text between one tag link and the
        next* for a size figure, rather than assuming any particular tag
        nesting. Returns [] on any failure so callers can fall back to a
        plain bare-name pull (which resolves to :latest)."""
        if "/" in base:
            # No verified tags-page URL convention for namespaced community
            # models (only the official /library/<name>/tags path is
            # confirmed) -- skip rather than guess.
            self.log(f"Tag listing is only available for official models; "
                      f"skipping variant lookup for '{base}'.")
            return []

        url = f"https://ollama.com/library/{urllib.parse.quote(base)}/tags"
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=6) as response:
                html = response.read().decode(errors="replace")
        except Exception as e:
            self.log(f"WARNING: Could not fetch tag list for '{base}': {e}")
            return []

        href_pattern = re.compile(rf'href="/library/{re.escape(base)}:([A-Za-z0-9._\-]+)"')
        matches = list(href_pattern.finditer(html))
        if not matches:
            self.log(f"WARNING: No tag variants found on {url} "
                      f"(page layout may have changed).")
            return []

        results = {}
        for i, m in enumerate(matches):
            pull_name = f"{base}:{m.group(1)}"
            if pull_name in results:
                continue
            chunk_end = matches[i + 1].start() if i + 1 < len(matches) else min(len(html), m.end() + 2000)
            chunk = html[m.end():chunk_end]
            size_match = re.search(r'(\d+(?:\.\d+)?)\s*(GB|MB)', chunk)
            results[pull_name] = f"{size_match.group(1)}{size_match.group(2)}" if size_match else "size unknown"

        return sorted(results.items())

    def toggle_leaf(self, node):
        """Single entry point for checking/unchecking a leaf, used by both
        Space and Enter. Tagged leaves (Local/Cloud Models, or a fully
        specified name) toggle immediately -- there's no ambiguity about
        which variant. Bare leaves (Available Models catalog entries) may
        have several pullable tags, so checking one kicks off the tag-picker
        flow instead of immediately assuming :latest."""
        name = node["name"]
        c = canonical(name)
        if c in self.checked:
            self.checked.discard(c)
            self.selected_tag_for_install.pop(c, None)
            return
        if ":" in name:
            self.checked.add(c)
            return
        self.begin_tag_selection(c)

    def begin_tag_selection(self, base):
        if self.is_fetching_tags:
            self.log("Already fetching a tag list; please wait.")
            return
        self.is_fetching_tags = True
        self.log(f"Fetching available tags for '{base}' ...")

        def worker():
            options = self.fetch_model_tags(base)
            with self.state_lock:
                if not options:
                    self.log(f"Defaulting '{base}' to its :latest tag "
                              f"(no specific variant list available).")
                    self.checked.add(base)
                    self.selected_tag_for_install.pop(base, None)
                elif len(options) == 1:
                    pull_name, size_str = options[0]
                    self.checked.add(base)
                    self.selected_tag_for_install[base] = pull_name
                    self.log(f"Only one variant available: {pull_name} "
                              f"({size_str}) -- selected automatically.")
                else:
                    self.log(f"Found {len(options)} variant(s) for '{base}'.")
                    self.tag_picker = TagPickerDialog(base, options)
                self.is_fetching_tags = False

        threading.Thread(target=worker, daemon=True).start()

    def get_model_url(self, leaf_name):
        """Resolve the ollama.com page for a model. Prefers the URL the
        ollamadb.dev API returned for that model; falls back to the known
        URL conventions: official models live at /library/<name>, namespaced
        community models live at /<namespace>/<name>. Tags are stripped
        since library pages are per-model, not per-tag."""
        c = canonical(leaf_name)
        if c in self.cloud_urls:
            return self.cloud_urls[c]
        if "/" in c:
            return f"https://ollama.com/{c}"
        return f"https://ollama.com/library/{c}"

    # ------------------------------------------------------------------ #
    # Rendering
    # ------------------------------------------------------------------ #
    def safe_addstr(self, y, x, text, attr=0):
        h, w = self.stdscr.getmaxyx()
        if y < 0 or y >= h or x >= w:
            return
        try:
            self.stdscr.addstr(y, x, text[: max(0, w - x)], attr)
        except curses.error:
            # Writing to the bottom-right cell raises even with correct
            # bounds on some terminals; never let rendering crash the app.
            pass

    def draw(self):
        self.stdscr.erase()  # erase(), not clear() -- avoids forced full
                              # physical repaint => eliminates periodic flicker
        h, w = self.stdscr.getmaxyx()

        title = " OLLAMA MODEL MANAGER "
        self.safe_addstr(0, max(0, (w - len(title)) // 2), title,
                          curses.color_pair(1) | curses.A_BOLD)
        self.safe_addstr(1, 0, "─" * (w - 1), curses.color_pair(1))

        tree_h = max(1, h - 12)
        if self.selected_row < self.tree_offset_y:
            self.tree_offset_y = self.selected_row
        elif self.selected_row >= self.tree_offset_y + tree_h:
            self.tree_offset_y = self.selected_row - tree_h + 1

        for i in range(tree_h):
            idx = self.tree_offset_y + i
            if idx >= len(self.visible_lines):
                break
            node, depth = self.visible_lines[idx]
            y = i + 2
            prefix = "  " * depth

            if node["is_group"]:
                symbol = "▼" if node["expanded"] else "▶"
                display_str = f"{prefix}{symbol} {node['name']}"
            else:
                mark = "x" if canonical(node["name"]) in self.checked else " "
                display_str = f"{prefix}[{mark}] {node['name']}"

            safe_display = display_str[: max(0, w - 15)]

            if self.focus == "tree" and idx == self.selected_row:
                self.safe_addstr(y, 0, safe_display.ljust(max(0, w - 1)), curses.color_pair(3))
            else:
                self.safe_addstr(y, 0, safe_display)

            if not node["is_group"]:
                variants = self.installed_variants(node["name"])
                if variants:
                    if ":" in node["name"]:
                        # Local Models / Cloud Models entries already carry
                        # their own tag in the display name -- don't repeat it.
                        tag = " [Installed]"
                    else:
                        # Available Models entries are bare/untagged in the
                        # catalog; surface which specific tag is actually
                        # installed (e.g. a ':cloud' proxy install vs a real
                        # local-weight download).
                        base = canonical(node["name"])
                        suffixes = sorted({v[len(base):] for v in variants if v[len(base):]})
                        tag = f" [Installed {', '.join(suffixes)}]" if suffixes else " [Installed]"
                    tag_x = len(safe_display) + 1
                    if tag_x + len(tag) < w:
                        self.safe_addstr(y, tag_x, tag, curses.color_pair(2) | curses.A_BOLD)

        # LOG SECTION
        self.safe_addstr(tree_h + 2, 0, "─" * (w - 1), curses.color_pair(1))
        self.safe_addstr(tree_h + 2, 2, " Logs ", curses.color_pair(1) | curses.A_BOLD)

        log_start_y = tree_h + 3
        log_h = max(1, h - log_start_y - 3)  # leave room for hint line + action bar

        drained = False
        while not self.log_queue.empty():
            self.logs.append(self.log_queue.get())
            drained = True
        if len(self.logs) > 500:
            self.logs = self.logs[-500:]

        for i, log_line in enumerate(self.logs[-log_h:]):
            self.safe_addstr(log_start_y + i, 1, log_line)

        # ACTION BAR: single Apply button + Quit button (req 5/6)
        self.safe_addstr(h - 2, 0, "─" * (w - 1), curses.color_pair(1))

        actions = [("Apply", "apply"), ("Quit", "quit")]
        btn_spacing = w // len(actions)
        for i, (label, key) in enumerate(actions):
            btn_str = f" [ {label} ] "
            x = (i * btn_spacing) + max(0, (btn_spacing - len(btn_str)) // 2)
            if self.focus == key:
                self.safe_addstr(h - 1, x, btn_str, curses.color_pair(3) | curses.A_BOLD)
            else:
                color = curses.color_pair(4) if key == "apply" else curses.color_pair(5)
                self.safe_addstr(h - 1, x, btn_str, color | curses.A_BOLD)

        hint = "Tab/Shift+Tab: panel | Space/Enter: toggle (picks a tag variant) | i: model info"
        self.safe_addstr(h - 3, 0, hint[: max(0, w - 1)], curses.A_DIM)

        if self.tag_picker is not None:
            self.draw_tag_picker()
        elif self.dialog is not None:
            self.draw_dialog()

        self.stdscr.noutrefresh()
        curses.doupdate()

    def draw_tag_picker(self):
        h, w = self.stdscr.getmaxyx()
        dlg = self.tag_picker
        header = f"Select a variant of '{dlg.base}' to install:"
        rows = [f"{name}  ({size})" for name, size in dlg.options]

        box_w = min(w - 4, max([len(header)] + [len(r) for r in rows]) + 6)
        max_visible = max(1, min(len(rows), h - 8))
        box_h = min(h - 4, max_visible + 4)
        y0 = max(0, (h - box_h) // 2)
        x0 = max(0, (w - box_w) // 2)

        for dy in range(box_h):
            self.safe_addstr(y0 + dy, x0, " " * box_w, curses.color_pair(3))

        self.safe_addstr(y0 + 1, x0 + 2, header[: box_w - 4],
                          curses.color_pair(3) | curses.A_BOLD)

        visible_opts = box_h - 3
        start = 0
        if dlg.selected >= visible_opts:
            start = dlg.selected - visible_opts + 1
        for i in range(visible_opts):
            idx = start + i
            if idx >= len(rows):
                break
            prefix = "> " if idx == dlg.selected else "  "
            attr = (curses.color_pair(2) | curses.A_BOLD) if idx == dlg.selected else curses.color_pair(3)
            self.safe_addstr(y0 + 2 + i, x0 + 2, (prefix + rows[idx])[: box_w - 4], attr)

        hint = "Up/Down: choose | Enter: select | Esc: cancel"
        self.safe_addstr(y0 + box_h - 1, x0 + 2, hint[: box_w - 4], curses.color_pair(3))

    def draw_dialog(self):
        h, w = self.stdscr.getmaxyx()
        lines = ["Confirm changes:"]
        if self.dialog.to_install:
            lines.append("Install:")
            lines += [f"  + {m}" for m in self.dialog.to_install]
        if self.dialog.to_uninstall:
            lines.append("Uninstall:")
            lines += [f"  - {m}" for m in self.dialog.to_uninstall]
        lines.append("")
        lines.append("[ Confirm ]      [ Cancel ]")

        box_w = min(w - 4, max(len(l) for l in lines) + 4)
        box_h = min(h - 4, len(lines) + 2)
        y0 = max(0, (h - box_h) // 2)
        x0 = max(0, (w - box_w) // 2)

        for dy in range(box_h):
            self.safe_addstr(y0 + dy, x0, " " * box_w, curses.color_pair(3))

        for i, line in enumerate(lines[: box_h - 2]):
            self.safe_addstr(y0 + 1 + i, x0 + 2, line[: box_w - 4], curses.color_pair(3))

        btn_y = y0 + box_h - 2
        confirm_x = x0 + 2
        cancel_x = x0 + 2 + len("[ Confirm ]") + 6
        self.safe_addstr(btn_y, confirm_x, "[ Confirm ]",
                          curses.color_pair(2) | curses.A_BOLD if self.dialog.selected == 0
                          else curses.color_pair(3))
        self.safe_addstr(btn_y, cancel_x, "[ Cancel ]",
                          curses.color_pair(5) | curses.A_BOLD if self.dialog.selected == 1
                          else curses.color_pair(3))

    # ------------------------------------------------------------------ #
    # Command execution
    # ------------------------------------------------------------------ #
    def run_command(self, cmd, model_name):
        full_cmd = ["ollama", cmd, model_name]
        self.log(f"$ {' '.join(full_cmd)}")
        try:
            process = subprocess.Popen(
                full_cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True
            )
        except FileNotFoundError:
            self.log("ERROR: 'ollama' executable not found on PATH. Aborting this step.")
            return

        for line in process.stdout:
            self.log(f"[{model_name}] {line.rstrip()}")

        process.wait()
        if process.returncode == 0:
            self.log(f"SUCCESS: {model_name} {cmd} completed.")
        else:
            self.log(f"ERROR: {model_name} {cmd} exited with code {process.returncode}.")

    def compute_diff(self):
        """Returns (to_install, to_uninstall) as lists of exact model strings
        to pass to `ollama pull` / `ollama rm`."""
        installed_canon_map = {}
        for m in self.installed_models:
            installed_canon_map.setdefault(canonical(m), []).append(m)
        installed_canonicals = set(installed_canon_map.keys())

        cloud_name_by_canon = {canonical(m): m for m in self.cloud_models}

        to_install_canon = self.checked - installed_canonicals
        to_uninstall_canon = installed_canonicals - self.checked

        to_install = [
            self.selected_tag_for_install.get(c, cloud_name_by_canon.get(c, c))
            for c in sorted(to_install_canon)
        ]
        to_uninstall = [name for c in sorted(to_uninstall_canon)
                         for name in installed_canon_map[c]]
        return to_install, to_uninstall

    def request_apply(self):
        if self.is_processing:
            self.log("Already processing a previous request; please wait.")
            return
        to_install, to_uninstall = self.compute_diff()
        if not to_install and not to_uninstall:
            self.log("No changes to apply -- checkbox state matches installed models.")
            return
        self.dialog = ConfirmDialog(to_install, to_uninstall)

    def confirm_apply(self):
        to_install = self.dialog.to_install
        to_uninstall = self.dialog.to_uninstall
        self.dialog = None
        self.is_processing = True
        self.log("--- Applying changes ---")

        def worker():
            for model in to_install:
                self.run_command("pull", model)
            for model in to_uninstall:
                self.run_command("rm", model)

            self.log("Refreshing local model list ...")
            with self.state_lock:
                self.installed_models = self.fetch_local_models()
                root_children = self.tree[0]["children"]
                local_group = next(g for g in root_children if g["name"] == "Local Models")
                cloud_group = next(g for g in root_children if g["name"] == "Cloud Models")
                local_group["children"] = [
                    {"name": m, "is_group": False}
                    for m in self.installed_models if not is_cloud_tag(m)
                ]
                cloud_group["children"] = [
                    {"name": m, "is_group": False}
                    for m in self.installed_models if is_cloud_tag(m)
                ]
                self.checked = {canonical(m) for m in self.installed_models}
                self.selected_tag_for_install.clear()
                self.update_visible_lines()
            self.log("--- All tasks completed ---")
            self.is_processing = False

        threading.Thread(target=worker, daemon=True).start()

    # ------------------------------------------------------------------ #
    # Input handling
    # ------------------------------------------------------------------ #
    def handle_dialog_input(self, key):
        if key in (curses.KEY_LEFT, curses.KEY_RIGHT, 9):
            self.dialog.selected = 1 - self.dialog.selected
        elif key in (10, 13):  # Enter
            if self.dialog.selected == 0:
                self.confirm_apply()
            else:
                self.log("Apply cancelled by user.")
                self.dialog = None
        elif key in (27,):  # Esc
            self.log("Apply cancelled by user.")
            self.dialog = None
        elif key in (ord('y'), ord('Y')):
            self.confirm_apply()
        elif key in (ord('n'), ord('N')):
            self.log("Apply cancelled by user.")
            self.dialog = None
        return True

    def handle_tag_picker_input(self, key):
        dlg = self.tag_picker
        if key == curses.KEY_UP and dlg.selected > 0:
            dlg.selected -= 1
        elif key == curses.KEY_DOWN and dlg.selected < len(dlg.options) - 1:
            dlg.selected += 1
        elif key in (10, 13):  # Enter confirms the highlighted variant
            pull_name, size_str = dlg.options[dlg.selected]
            self.checked.add(dlg.base)
            self.selected_tag_for_install[dlg.base] = pull_name
            self.log(f"Selected {pull_name} ({size_str}) for installation.")
            self.tag_picker = None
        elif key in (27, ord('n'), ord('N')):  # Esc/cancel
            self.log(f"Tag selection cancelled for '{dlg.base}'.")
            self.tag_picker = None
        return True

    def handle_input(self, key):
        """Returns True to keep running, False to quit."""
        if self.tag_picker is not None:
            return self.handle_tag_picker_input(key)

        if self.dialog is not None:
            return self.handle_dialog_input(key)

        if self.is_fetching_tags:
            if key in (ord('q'), ord('Q')):
                self.log("Fetching tag list; please wait a moment.")
            return True

        if self.is_processing:
            if key in (ord('q'), ord('Q')):
                self.log("Cannot quit while a pull/remove is in progress.")
            return True

        if key == 9:      # Tab
            order = ["tree", "apply", "quit"]
            self.focus = order[(order.index(self.focus) + 1) % len(order)]
            return True
        if key == curses.KEY_BTAB:  # Shift-Tab
            order = ["tree", "apply", "quit"]
            self.focus = order[(order.index(self.focus) - 1) % len(order)]
            return True

        if self.focus == "tree":
            if key == curses.KEY_UP and self.selected_row > 0:
                self.selected_row -= 1
            elif key == curses.KEY_DOWN and self.selected_row < len(self.visible_lines) - 1:
                self.selected_row += 1
            elif key == ord(' '):
                node, _ = self.visible_lines[self.selected_row]
                if not node["is_group"]:
                    self.toggle_leaf(node)
            elif key in (10, 13):  # Enter
                node, _ = self.visible_lines[self.selected_row]
                if node["is_group"]:
                    node["expanded"] = not node["expanded"]
                    self.update_visible_lines()
                else:
                    self.toggle_leaf(node)
            elif key == curses.KEY_RIGHT:
                node, depth = self.visible_lines[self.selected_row]
                if node["is_group"]:
                    if not node["expanded"]:
                        node["expanded"] = True
                        self.update_visible_lines()
                    elif node["children"]:
                        # Already expanded -- jump into the first child.
                        self.selected_row += 1
            elif key == curses.KEY_LEFT:
                node, depth = self.visible_lines[self.selected_row]
                if node["is_group"] and node["expanded"]:
                    node["expanded"] = False
                    self.update_visible_lines()
                elif depth > 0:
                    # Leaf or already-collapsed group -- jump up to parent row.
                    for i in range(self.selected_row - 1, -1, -1):
                        _, d = self.visible_lines[i]
                        if d == depth - 1:
                            self.selected_row = i
                            break
            elif key in (ord('i'), ord('I')):
                node, _ = self.visible_lines[self.selected_row]
                if not node["is_group"]:
                    url = self.get_model_url(node["name"])
                    self.log(f"Opening browser: {url}")
                    try:
                        opened = webbrowser.open(url, new=2)
                        if not opened:
                            self.log("WARNING: webbrowser module reported no browser controller "
                                      "was available on this system.")
                    except Exception as e:
                        self.log(f"ERROR: Failed to open browser: {e}")

        elif self.focus == "apply":
            if key in (10, 13, ord(' ')):
                self.request_apply()

        elif self.focus == "quit":
            if key in (10, 13, ord(' ')):
                return False

        if key in (ord('q'), ord('Q')):
            return False

        return True

    def main_loop(self):
        self.stdscr.timeout(100)
        running = True
        # Initial paint.
        self.draw()
        while running:
            key = self.stdscr.getch()
            changed = False

            if key != -1:
                running = self.handle_input(key)
                changed = True
            if not self.log_queue.empty():
                changed = True
            if self.is_processing:
                # Keep polling for streamed subprocess output even without
                # keypresses, but only repaint -- no clear/flash -- when the
                # log queue actually produced something (handled above) or
                # periodically at a low duty cycle to show liveness.
                changed = changed or True

            if changed:
                self.draw()


def main(stdscr):
    app = OllamaTUI(stdscr)
    app.main_loop()


if __name__ == "__main__":
    try:
        curses.wrapper(main)
    except KeyboardInterrupt:
        pass
