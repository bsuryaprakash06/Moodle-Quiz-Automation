"""
Activity Selector Module for Poodle LMS Automation.

Provides an interactive GUI Checkbox Dialog (via Tkinter) and a CLI fallback
to prompt the user on which activities to automate, featuring:
- Master '(all)' option to select/deselect all activities
- Similarity-based grouping (Read-Aloud, H5P, WordCards, Solo, Generic)
- Group-level toggles
- Filter/search support
- Pending-only quick filter
- Seamless dynamic theme switching: Dark (Default) / Light / Transparent (Glass)
- Seamless CLI fallback for headless or non-GUI environments
"""

import sys
import re
from typing import Optional

GROUP_DEFINITIONS = [
    {
        "id": "readaloud",
        "name": "Read - Aloud",
        "description": "Speech pronunciation and reading exercises",
        "match_types": ["readaloud"]
    },
    {
        "id": "hvp",
        "name": "H5P / Interactive Video",
        "description": "Interactive video queries, drag-and-drop & quiz questions",
        "match_types": ["hvp", "h5pactivity"]
    },
    {
        "id": "wordcards",
        "name": "WordCards",
        "description": "Vocabulary recognition and flashcard drills",
        "match_types": ["wordcards"]
    },
    {
        "id": "solo",
        "name": "Solo Speaking",
        "description": "Continuous speech monologue and speaking prompts",
        "match_types": ["solo"]
    },
    {
        "id": "quiz",
        "name": "Quiz & Assessments",
        "description": "Online quizzes, multiple choice and practice tests",
        "match_types": ["quiz"]
    },
    {
        "id": "generic",
        "name": "Other Activities",
        "description": "Standard Moodle course resources and modules",
        "match_types": []
    }
]

THEME_ORDER = ["dark", "light", "transparent"]

THEME_LABELS = {
    "dark": "🌙 Dark",
    "light": "☀️ Light",
    "transparent": "🪟 Transparent"
}

THEME_PALETTES = {
    "dark": {
        "alpha": 1.0,
        "bg_dark": "#181825",
        "card_bg": "#1e1e2e",
        "header_bg": "#28283d",
        "row_bg": "#1e1e2e",
        "row_border": "#313244",
        "text_primary": "#cdd6f4",
        "text_muted": "#a6adc8",
        "accent_blue": "#89b4fa",
        "entry_bg": "#26263a",
        "entry_fg": "#ffffff",
        "btn_bg": "#313244",
        "btn_fg": "#cdd6f4",
        "btn_active": "#45475a",
        "primary_bg": "#3b82f6",
        "primary_fg": "#ffffff",
        "restricted_bg": "#451a24",
        "restricted_fg": "#f38ba8",
        "restricted_border": "#f38ba8",
        "badge_done": {"bg": "#1e3a2f", "fg": "#a6e3a1"},
        "badge_retry": {"bg": "#4a1a24", "fg": "#f38ba8"},
        "badge_pending": {"bg": "#3d3215", "fg": "#f9e2af"}
    },
    "light": {
        "alpha": 1.0,
        "bg_dark": "#f8f9fa",
        "card_bg": "#ffffff",
        "header_bg": "#e9ecef",
        "row_bg": "#ffffff",
        "row_border": "#dee2e6",
        "text_primary": "#212529",
        "text_muted": "#6c757d",
        "accent_blue": "#0056b3",
        "entry_bg": "#ffffff",
        "entry_fg": "#212529",
        "btn_bg": "#e2e6ea",
        "btn_fg": "#212529",
        "btn_active": "#dbe0e5",
        "primary_bg": "#0d6efd",
        "primary_fg": "#ffffff",
        "restricted_bg": "#ffcccc",
        "restricted_fg": "#721c24",
        "restricted_border": "#dc3545",
        "badge_done": {"bg": "#d4edda", "fg": "#155724"},
        "badge_retry": {"bg": "#f8d7da", "fg": "#721c24"},
        "badge_pending": {"bg": "#fff3cd", "fg": "#856404"}
    },
    "transparent": {
        "alpha": 0.85,
        "bg_dark": "#11111b",
        "card_bg": "#181825",
        "header_bg": "#1e1e2e",
        "row_bg": "#181825",
        "row_border": "#45475a",
        "text_primary": "#ffffff",
        "text_muted": "#bac2de",
        "accent_blue": "#89b4fa",
        "entry_bg": "#1e1e2e",
        "entry_fg": "#ffffff",
        "btn_bg": "#313244",
        "btn_fg": "#cdd6f4",
        "btn_active": "#45475a",
        "primary_bg": "#3b82f6",
        "primary_fg": "#ffffff",
        "restricted_bg": "#451a24",
        "restricted_fg": "#f38ba8",
        "restricted_border": "#f38ba8",
        "badge_done": {"bg": "#1e3a2f", "fg": "#a6e3a1"},
        "badge_retry": {"bg": "#4a1a24", "fg": "#f38ba8"},
        "badge_pending": {"bg": "#3d3215", "fg": "#f9e2af"}
    }
}


def classify_activity(act: dict) -> str:
    """Returns the group ID for an activity based on its type or URL."""
    act_type = str(act.get("type", "")).lower()
    url = str(act.get("url", "")).lower()

    for grp in GROUP_DEFINITIONS:
        for t in grp["match_types"]:
            if t == act_type or f"/mod/{t}/" in url:
                return grp["id"]
    return "generic"


def group_activities(activities: list[dict]) -> list[dict]:
    """
    Groups activities into structured similarity buckets with global 1-based indexing.
    Returns a list of group dictionaries.
    """
    buckets = {grp["id"]: [] for grp in GROUP_DEFINITIONS}

    for idx, act in enumerate(activities, 1):
        grp_id = classify_activity(act)
        buckets[grp_id].append((idx, act))

    result = []
    for grp in GROUP_DEFINITIONS:
        items = buckets[grp["id"]]
        if items:
            result.append({
                "id": grp["id"],
                "name": grp["name"],
                "description": grp["description"],
                "items": items
            })
    return result


class ActivitySelectorDialog:
    """Tkinter-based GUI Checkbox dialog with grouping, filter, and dynamic theme switching."""

    def __init__(self, activities: list[dict], course_name: str = "", tracker=None):
        self.activities = activities
        self.course_name = course_name or "Moodle Course"
        self.tracker = tracker
        self.grouped = group_activities(activities)
        self.selected_activities: list[dict] = []
        self.cancelled = False

        # Theme state (Default: Dark)
        self.current_theme = "dark"
        self.root = None
        self.style = None
        self.theme_btn = None
        self.canvas = None
        self.scrollable_frame = None
        self.search_entry = None
        self.filter_lbl = None
        self.container_frames = []
        self.group_ui_items = []
        self.activity_ui_items = []

        # GUI Tkinter variables
        self.all_var = None
        self.item_vars: dict[int, any] = {}          # idx -> BooleanVar
        self.group_vars: dict[str, any] = {}         # grp_id -> BooleanVar
        self.item_widgets: dict[int, any] = {}       # idx -> (row_widget, act) for filtering
        self.group_widgets: dict[str, any] = {}      # grp_id -> frame widget for filtering
        self.highlight_widgets: dict[int, list] = {} # idx -> list of widgets to highlight
        self.selection_label = None

    def show(self) -> Optional[list[dict]]:
        """Displays the dialog and returns the selected activities, or None if cancelled."""
        try:
            import tkinter as tk
            from tkinter import ttk
        except ImportError:
            return None

        root = tk.Tk()
        self.root = root
        root.title(f"Select Activities - {self.course_name}")
        root.geometry("820x680")
        root.minsize(680, 480)

        # Style configuration
        style = ttk.Style(root)
        self.style = style
        try:
            style.theme_use("clam")
        except Exception:
            pass

        self.container_frames.clear()
        self.group_ui_items.clear()
        self.activity_ui_items.clear()

        # Setup variables
        self.all_var = tk.BooleanVar(value=True)

        for grp in self.grouped:
            self.group_vars[grp["id"]] = tk.BooleanVar(value=True)
            for idx, act in grp["items"]:
                is_restricted = act.get("is_restricted", False)
                self.item_vars[idx] = tk.BooleanVar(value=not is_restricted)
            all_in_grp = all(self.item_vars[i].get() for i, _ in grp["items"])
            self.group_vars[grp["id"]].set(all_in_grp)
        all_selected = all(v.get() for v in self.item_vars.values())
        self.all_var.set(all_selected)

        # 1. Top Header Frame
        header_frame = tk.Frame(root, padx=16, pady=10)
        header_frame.pack(fill=tk.X)
        self.container_frames.append(header_frame)

        header_left = tk.Frame(header_frame)
        header_left.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self.container_frames.append(header_left)

        title_lbl = ttk.Label(
            header_left,
            text=f"📚 Course: {self.course_name}",
            style="Header.TLabel"
        )
        title_lbl.pack(anchor=tk.W)

        sub_text = f"Discovered {len(self.activities)} activities across {len(self.grouped)} categories. Select the activities you wish to automate:"
        sub_lbl = ttk.Label(header_left, text=sub_text, style="SubHeader.TLabel")
        sub_lbl.pack(anchor=tk.W, pady=(2, 4))

        # Right: Theme Toggle Button (Light / Dark / Transparent)
        header_right = tk.Frame(header_frame)
        header_right.pack(side=tk.RIGHT, anchor=tk.E, padx=(8, 0))
        self.container_frames.append(header_right)

        self.theme_btn = ttk.Button(
            header_right,
            text=f"🎨 {THEME_LABELS[self.current_theme]}",
            style="Action.TButton",
            command=self._cycle_theme
        )
        self.theme_btn.pack(side=tk.RIGHT)

        # 2. Controls & Filter Bar
        ctrl_frame = tk.Frame(root, padx=16, pady=4)
        ctrl_frame.pack(fill=tk.X)
        self.container_frames.append(ctrl_frame)

        # Master (all) Checkbox
        all_cb = ttk.Checkbutton(
            ctrl_frame,
            text="(all)  Select / Deselect All Activities",
            variable=self.all_var,
            command=self._on_toggle_all,
            style="AllCheck.TCheckbutton"
        )
        all_cb.pack(side=tk.LEFT)

        # Quick action buttons
        btn_frame = tk.Frame(ctrl_frame)
        btn_frame.pack(side=tk.RIGHT)
        self.container_frames.append(btn_frame)

        ttk.Button(btn_frame, text="Select Pending Only", style="Action.TButton", command=self._select_pending_only).pack(side=tk.LEFT, padx=3)
        ttk.Button(btn_frame, text="Select All", style="Action.TButton", command=lambda: self._set_all(True)).pack(side=tk.LEFT, padx=3)
        ttk.Button(btn_frame, text="Clear All", style="Action.TButton", command=lambda: self._set_all(False)).pack(side=tk.LEFT, padx=3)

        # Search / Filter Bar
        search_frame = tk.Frame(root, padx=16, pady=4)
        search_frame.pack(fill=tk.X)
        self.container_frames.append(search_frame)

        self.filter_lbl = tk.Label(search_frame, text="🔍 Filter:", font=("Segoe UI", 9))
        self.filter_lbl.pack(side=tk.LEFT, padx=(0, 6))

        self.search_var = tk.StringVar()
        self.search_var.trace_add("write", lambda *args: self._filter_activities())
        self.search_entry = tk.Entry(
            search_frame,
            textvariable=self.search_var,
            font=("Segoe UI", 9),
            relief=tk.FLAT,
            highlightthickness=1
        )
        self.search_entry.pack(side=tk.LEFT, fill=tk.X, expand=True)

        ttk.Button(search_frame, text="✕ Clear", width=8, style="Action.TButton", command=lambda: self.search_var.set("")).pack(side=tk.LEFT, padx=6)

        # Separator line
        sep = tk.Frame(root, height=1)
        sep.pack(fill=tk.X, padx=16, pady=6)
        self.container_frames.append(sep)

        # 3. Scrollable Activities Canvas
        container = tk.Frame(root, padx=16, pady=4)
        container.pack(fill=tk.BOTH, expand=True)
        self.container_frames.append(container)

        self.canvas = tk.Canvas(container, borderwidth=0, highlightthickness=0)
        scrollbar = ttk.Scrollbar(container, orient=tk.VERTICAL, command=self.canvas.yview)
        self.scrollable_frame = tk.Frame(self.canvas)

        def _on_frame_configure(event):
            self.canvas.configure(scrollregion=self.canvas.bbox("all"))

        self.scrollable_frame.bind("<Configure>", _on_frame_configure)
        canvas_window = self.canvas.create_window((0, 0), window=self.scrollable_frame, anchor="nw")

        def _on_canvas_configure(event):
            self.canvas.itemconfig(canvas_window, width=event.width)

        self.canvas.bind("<Configure>", _on_canvas_configure)
        self.canvas.configure(yscrollcommand=scrollbar.set)

        # Mousewheel scrolling support for Windows
        def _on_mousewheel(event):
            self.canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")

        self.canvas.bind_all("<MouseWheel>", _on_mousewheel)

        self.canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

        # Populate Grouped Activities
        for grp in self.grouped:
            grp_id = grp["id"]
            grp_box = tk.Frame(self.scrollable_frame, bd=1, relief=tk.SOLID, padx=10, pady=8, highlightthickness=1)
            grp_box.pack(fill=tk.X, expand=True, pady=6, padx=2)
            self.group_widgets[grp_id] = grp_box

            # Group Header
            gh_frame = tk.Frame(grp_box, padx=8, pady=4)
            gh_frame.pack(fill=tk.X)

            grp_cb = ttk.Checkbutton(
                gh_frame,
                text=f"{grp['name']} ({len(grp['items'])} activities)",
                variable=self.group_vars[grp_id],
                command=lambda gid=grp_id: self._on_toggle_group(gid),
                style="GroupHeader.TCheckbutton"
            )
            grp_cb.pack(side=tk.LEFT)

            desc_lbl = tk.Label(
                gh_frame,
                text=grp["description"],
                font=("Segoe UI", 8, "italic")
            )
            desc_lbl.pack(side=tk.RIGHT, padx=6)

            # Items inside group
            items_frame = tk.Frame(grp_box, pady=4)
            items_frame.pack(fill=tk.X)

            self.group_ui_items.append((grp_box, gh_frame, grp_cb, desc_lbl, items_frame))

            for idx, act in grp["items"]:
                item_row = tk.Frame(items_frame, padx=8, pady=4, bd=1, relief=tk.SOLID, highlightthickness=1)
                item_row.pack(fill=tk.X, pady=2)
                self.item_widgets[idx] = (item_row, act)

                # Checkbox
                cb = ttk.Checkbutton(
                    item_row,
                    variable=self.item_vars[idx],
                    style="ItemCheck.TCheckbutton",
                    command=lambda i=idx, gid=grp_id: self._on_toggle_item(i, gid)
                )
                cb.pack(side=tk.LEFT, padx=(0, 6))

                # Index & Title Label
                num_tag = f"#{idx:02d}"
                title_text = f"{num_tag}  [{act.get('type', 'act')}]  {act['title']}"
                title_lbl = tk.Label(
                    item_row,
                    text=title_text,
                    font=("Segoe UI", 9),
                    anchor="w",
                    cursor="hand2"
                )
                title_lbl.pack(side=tk.LEFT, fill=tk.X, expand=True)
                title_lbl.bind("<Button-1>", lambda e, i=idx, gid=grp_id: self._toggle_single_item(i, gid))

                self.highlight_widgets[idx] = [item_row, title_lbl, cb]

                # Status Badge
                is_done = False
                score_str = ""
                raw_score = self.tracker.get_score(act["url"]) if self.tracker else ""
                if self.tracker and self.tracker.is_completed(act["url"]):
                    is_done = True
                    score_str = str(raw_score)

                if is_done:
                    badge_text = f"✓ COMPLETED ({score_str})" if score_str else "✓ COMPLETED"
                elif raw_score:
                    badge_text = f"● RETRY ({raw_score})"
                else:
                    badge_text = "● PENDING"

                badge_lbl = tk.Label(
                    item_row,
                    text=badge_text,
                    font=("Segoe UI", 8, "bold"),
                    padx=6,
                    pady=1
                )
                badge_lbl.pack(side=tk.RIGHT, padx=4)

                self.activity_ui_items.append((idx, item_row, title_lbl, cb, badge_lbl, act))

        # 4. Bottom Footer & Action Buttons
        footer_frame = tk.Frame(root, padx=16, pady=12)
        footer_frame.pack(fill=tk.X)
        self.container_frames.append(footer_frame)

        self.selection_label = ttk.Label(
            footer_frame,
            text="",
            style="Selection.TLabel"
        )
        self.selection_label.pack(side=tk.LEFT)

        action_box = tk.Frame(footer_frame)
        action_box.pack(side=tk.RIGHT)
        self.container_frames.append(action_box)

        cancel_btn = ttk.Button(
            action_box,
            text="Cancel (Esc)",
            style="Action.TButton",
            command=lambda: self._close(cancelled=True, root=root)
        )
        cancel_btn.pack(side=tk.LEFT, padx=6)

        start_btn = ttk.Button(
            action_box,
            text="🚀 Start Automation (Enter)",
            style="Primary.TButton",
            command=lambda: self._close(cancelled=False, root=root)
        )
        start_btn.pack(side=tk.LEFT)

        # Apply Default Theme (Dark)
        self.apply_theme(self.current_theme)

        # Keyboard bindings
        root.bind("<Return>", lambda e: self._close(cancelled=False, root=root))
        root.bind("<Escape>", lambda e: self._close(cancelled=True, root=root))

        self._update_selection_count()

        # Center Window on screen
        root.update_idletasks()
        w = root.winfo_width()
        h = root.winfo_height()
        sw = root.winfo_screenwidth()
        sh = root.winfo_screenheight()
        x = max(0, (sw - w) // 2)
        y = max(0, (sh - h) // 2)
        root.geometry(f"{w}x{h}+{x}+{y}")

        # Bring dialog to front
        try:
            root.lift()
            root.attributes("-topmost", True)
            root.after(300, lambda: root.attributes("-topmost", False))
            root.focus_force()
        except Exception:
            pass

        root.mainloop()

        try:
            canvas.unbind_all("<MouseWheel>")
        except Exception:
            pass

        if self.cancelled:
            return None
        return self.selected_activities

    def _cycle_theme(self):
        """Cycles through Dark -> Light -> Transparent -> Dark."""
        curr_idx = THEME_ORDER.index(self.current_theme)
        next_theme = THEME_ORDER[(curr_idx + 1) % len(THEME_ORDER)]
        self.apply_theme(next_theme)

    def apply_theme(self, theme_name: str):
        """Applies colors, styles, and window transparency for the given theme."""
        if theme_name not in THEME_PALETTES:
            return
        self.current_theme = theme_name
        p = THEME_PALETTES[theme_name]

        # 1. Window & Opacity
        if self.root:
            self.root.configure(background=p["bg_dark"])
            try:
                self.root.attributes("-alpha", p["alpha"])
            except Exception:
                pass

        # 2. Reconfigure TTK styles
        if self.style:
            try:
                self.style.configure(".", background=p["bg_dark"], foreground=p["text_primary"])
                self.style.configure("TFrame", background=p["bg_dark"])
                self.style.configure("TLabel", background=p["bg_dark"], foreground=p["text_primary"])
                self.style.configure("TSeparator", background=p["row_border"])

                self.style.configure("Header.TLabel", font=("Segoe UI", 13, "bold"), foreground=p["text_primary"], background=p["bg_dark"])
                self.style.configure("SubHeader.TLabel", font=("Segoe UI", 9), foreground=p["text_muted"], background=p["bg_dark"])
                self.style.configure("GroupHeader.TCheckbutton", font=("Segoe UI", 10, "bold"), foreground=p["accent_blue"], background=p["header_bg"])
                self.style.map("GroupHeader.TCheckbutton", background=[("active", p["header_bg"])], foreground=[("active", p["accent_blue"])])

                self.style.configure("AllCheck.TCheckbutton", font=("Segoe UI", 11, "bold"), foreground=p["text_primary"], background=p["bg_dark"])
                self.style.map("AllCheck.TCheckbutton", background=[("active", p["bg_dark"])], foreground=[("active", p["text_primary"])])

                self.style.configure("ItemCheck.TCheckbutton", background=p["row_bg"], foreground=p["text_primary"])
                self.style.map("ItemCheck.TCheckbutton", background=[("active", p["row_bg"])], foreground=[("active", p["text_primary"])])

                self.style.configure("Action.TButton", font=("Segoe UI", 9), background=p["btn_bg"], foreground=p["btn_fg"], borderwidth=1, focuscolor="none")
                self.style.map("Action.TButton", background=[("active", p["btn_active"])], foreground=[("active", p["btn_fg"])])

                self.style.configure("Primary.TButton", font=("Segoe UI", 10, "bold"), background=p["primary_bg"], foreground=p["primary_fg"], borderwidth=0, focuscolor="none")
                self.style.map("Primary.TButton", background=[("active", p["primary_bg"])], foreground=[("active", p["primary_fg"])])

                self.style.configure("Selection.TLabel", font=("Segoe UI", 10, "bold"), foreground=p["accent_blue"], background=p["bg_dark"])
            except Exception:
                pass

        # 3. Theme Toggle Button Label
        if self.theme_btn:
            self.theme_btn.config(text=f"🎨 {THEME_LABELS[theme_name]}")

        # 4. Container Frames & Canvas
        for frame in self.container_frames:
            try:
                frame.configure(background=p["bg_dark"])
            except Exception:
                pass

        if self.canvas:
            self.canvas.configure(background=p["bg_dark"])
        if self.scrollable_frame:
            self.scrollable_frame.configure(background=p["bg_dark"])

        # 5. Search Entry & Filter Label
        if self.search_entry:
            try:
                self.search_entry.configure(
                    bg=p["entry_bg"],
                    fg=p["entry_fg"],
                    insertbackground=p["entry_fg"],
                    highlightbackground=p["row_border"],
                    highlightcolor=p["accent_blue"]
                )
            except Exception:
                pass

        if self.filter_lbl:
            try:
                self.filter_lbl.configure(foreground=p["text_primary"], background=p["bg_dark"])
            except Exception:
                pass

        # 6. Group UI components
        for grp_box, gh_frame, grp_cb, desc_lbl, items_frame in self.group_ui_items:
            try:
                grp_box.configure(background=p["card_bg"], highlightbackground=p["row_border"])
                gh_frame.configure(background=p["header_bg"])
                desc_lbl.configure(fg=p["text_muted"], bg=p["header_bg"])
                items_frame.configure(background=p["card_bg"])
            except Exception:
                pass

        # 7. Activity Items & Badges
        for idx, item_row, title_lbl, cb, badge_lbl, act in self.activity_ui_items:
            try:
                is_restricted = act.get("is_restricted", False)
                is_checked = self.item_vars[idx].get()
                bg_color = p["restricted_bg"] if (is_restricted and is_checked) else p["row_bg"]
                border_color = p["restricted_border"] if (is_restricted and is_checked) else p["row_border"]
                fg_title = p["restricted_fg"] if (is_restricted and is_checked) else p["text_primary"]

                item_row.configure(background=bg_color, highlightbackground=border_color)
                title_lbl.configure(bg=bg_color, fg=fg_title)

                # Badge update
                is_done = False
                raw_score = self.tracker.get_score(act["url"]) if self.tracker else ""
                if self.tracker and self.tracker.is_completed(act["url"]):
                    is_done = True

                if is_done:
                    badge_lbl.configure(bg=p["badge_done"]["bg"], fg=p["badge_done"]["fg"])
                elif raw_score:
                    badge_lbl.configure(bg=p["badge_retry"]["bg"], fg=p["badge_retry"]["fg"])
                else:
                    badge_lbl.configure(bg=p["badge_pending"]["bg"], fg=p["badge_pending"]["fg"])
            except Exception:
                pass

        self._update_selection_count()

    def _update_row_highlight(self, idx: int):
        if idx not in self.highlight_widgets:
            return
        act = self.item_widgets[idx][1]
        is_restricted = act.get("is_restricted", False)
        p = THEME_PALETTES.get(self.current_theme, THEME_PALETTES["dark"])
        if is_restricted:
            is_checked = self.item_vars[idx].get()
            bg_color = p["restricted_bg"] if is_checked else p["row_bg"]
            fg_color = p["restricted_fg"] if is_checked else p["text_primary"]
            border_color = p["restricted_border"] if is_checked else p["row_border"]
            item_frame = self.highlight_widgets[idx][0]
            title_lbl = self.highlight_widgets[idx][1]
            item_frame.config(bg=bg_color, highlightbackground=border_color)
            title_lbl.config(bg=bg_color, fg=fg_color)

    def _toggle_single_item(self, idx: int, group_id: str):
        var = self.item_vars.get(idx)
        if var:
            var.set(not var.get())
            self._on_toggle_item(idx, group_id)

    def _on_toggle_all(self):
        val = self.all_var.get()
        self._set_all(val)

    def _set_all(self, val: bool):
        self.all_var.set(val)
        for g_var in self.group_vars.values():
            g_var.set(val)
        for idx, i_var in self.item_vars.items():
            i_var.set(val)
            self._update_row_highlight(idx)
        self._update_selection_count()

    def _select_pending_only(self):
        for idx, act in enumerate(self.activities, 1):
            is_done = self.tracker.is_completed(act["url"]) if self.tracker else False
            self.item_vars[idx].set(not is_done)
            self._update_row_highlight(idx)

        for grp in self.grouped:
            grp_id = grp["id"]
            all_in_grp = all(self.item_vars[i].get() for i, _ in grp["items"])
            self.group_vars[grp_id].set(all_in_grp)

        all_selected = all(v.get() for v in self.item_vars.values())
        self.all_var.set(all_selected)
        self._update_selection_count()

    def _on_toggle_group(self, grp_id: str):
        val = self.group_vars[grp_id].get()
        for grp in self.grouped:
            if grp["id"] == grp_id:
                for idx, _ in grp["items"]:
                    self.item_vars[idx].set(val)
                    self._update_row_highlight(idx)
                break
        all_selected = all(v.get() for v in self.item_vars.values())
        self.all_var.set(all_selected)
        self._update_selection_count()

    def _on_toggle_item(self, idx: int, grp_id: str):
        self._update_row_highlight(idx)
        for grp in self.grouped:
            if grp["id"] == grp_id:
                all_grp = all(self.item_vars[i].get() for i, _ in grp["items"])
                self.group_vars[grp_id].set(all_grp)
                break
        all_selected = all(v.get() for v in self.item_vars.values())
        self.all_var.set(all_selected)
        self._update_selection_count()

    def _filter_activities(self):
        query = (self.search_var.get() or "").strip().lower()
        for grp in self.grouped:
            grp_id = grp["id"]
            any_visible = False
            for idx, act in grp["items"]:
                row_widget, _ = self.item_widgets[idx]
                text_match = (
                    query in act["title"].lower()
                    or query in act.get("type", "").lower()
                    or query in f"#{idx}"
                )
                if text_match or not query:
                    row_widget.pack(fill="x", pady=2)
                    any_visible = True
                else:
                    row_widget.pack_forget()

            grp_widget = self.group_widgets.get(grp_id)
            if grp_widget:
                if any_visible:
                    grp_widget.pack(fill="x", expand=True, pady=6, padx=2)
                else:
                    grp_widget.pack_forget()

    def _update_selection_count(self):
        if not self.selection_label:
            return
        total = len(self.activities)
        selected = sum(1 for v in self.item_vars.values() if v.get())
        pending_count = 0
        if self.tracker:
            pending_count = sum(
                1 for i, act in enumerate(self.activities, 1)
                if self.item_vars[i].get() and not self.tracker.is_completed(act["url"])
            )
            self.selection_label.config(
                text=f"Selected: {selected} / {total} activities ({pending_count} pending)"
            )
        else:
            self.selection_label.config(text=f"Selected: {selected} / {total} activities")

    def _close(self, cancelled: bool, root):
        self.cancelled = cancelled
        if not cancelled:
            self.selected_activities = [
                act for idx, act in enumerate(self.activities, 1)
                if self.item_vars[idx].get()
            ]
        root.destroy()


def parse_cli_selection(user_input: str, total_count: int, activities: list[dict], tracker=None) -> list[int]:
    """
    Parses CLI user input into a list of 1-based activity indices.
    Supports:
    - 'all', '0', '' -> all indices
    - 'pending' -> indices of activities not marked as completed
    - group keywords ('readaloud', 'hvp', 'wordcards', 'solo')
    - comma-separated numbers and ranges ('1, 3, 5-8')
    """
    raw = user_input.strip().lower()

    if not raw or raw in ("all", "0", "*"):
        return list(range(1, total_count + 1))

    if raw == "pending":
        if not tracker:
            return list(range(1, total_count + 1))
        return [
            i for i, act in enumerate(activities, 1)
            if not tracker.is_completed(act["url"])
        ]

    # Check for group match
    for grp in GROUP_DEFINITIONS:
        if raw == grp["id"] or raw == grp["name"].lower() or raw.replace(" ", "") == grp["id"]:
            matching_indices = []
            for i, act in enumerate(activities, 1):
                if classify_activity(act) == grp["id"]:
                    matching_indices.append(i)
            if matching_indices:
                return matching_indices

    # Parse numerical expressions like '1, 3, 5-8'
    indices = set()
    tokens = [t.strip() for t in raw.split(",") if t.strip()]
    for tok in tokens:
        if "-" in tok:
            parts = tok.split("-", 1)
            try:
                start, end = int(parts[0].strip()), int(parts[1].strip())
                for i in range(min(start, end), max(start, end) + 1):
                    if 1 <= i <= total_count:
                        indices.add(i)
            except ValueError:
                pass
        else:
            try:
                val = int(tok)
                if 1 <= val <= total_count:
                    indices.add(val)
            except ValueError:
                pass

    return sorted(list(indices))


def cli_activity_selector(activities: list[dict], course_name: str = "", tracker=None) -> list[dict]:
    """Console-based fallback selector with similarity grouping and '(all)' option."""
    grouped = group_activities(activities)
    total_count = len(activities)

    print("\n" + "=" * 68)
    print(f"ACTIVITIES SELECTION - {course_name or 'Course'}")
    print("=" * 68)
    print(f"Extracted {total_count} activities across {len(grouped)} groups.\n")
    print("  [0] (all) - Select All Activities Across All Groups")

    for grp in grouped:
        print(f"\n--- {grp['name']} ({len(grp['items'])} activities) ---")
        for idx, act in grp["items"]:
            status_text = "[PENDING]"
            raw_score = tracker.get_score(act["url"]) if tracker else ""
            if tracker and tracker.is_completed(act["url"]):
                status_text = f"[COMPLETED | Score: {raw_score}]"
            elif raw_score:
                status_text = f"[RETRY NEEDED | Score: {raw_score}]"
            print(f"  [{idx:2d}] [{act.get('type', 'act'):10s}] {act['title']} {status_text}")

    print("\n" + "-" * 68)
    print("Selection Options:")
    print("  - Type 'all' or '0' (or press Enter) to automate ALL activities")
    print("  - Type 'pending' to automate only incomplete activities")
    print("  - Type a group name (e.g. 'readaloud', 'hvp', 'wordcards', 'solo')")
    print("  - Type numbers or ranges (e.g. '1, 3, 5' or '1-4')")
    print("  - Type 'none' or 'q' to cancel and exit")
    print("-" * 68)

    while True:
        try:
            choice = input("\nEnter your selection [default: (all)]: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n[Activity Selector] Operation cancelled by user.")
            return []

        if choice.lower() in ("none", "q", "cancel", "exit"):
            print("[Activity Selector] Skipping activities upon user request.")
            return []

        selected_indices = parse_cli_selection(choice, total_count, activities, tracker)
        if selected_indices:
            selected = [activities[i - 1] for i in selected_indices]
            print(f"\n[Activity Selector] Selected {len(selected)} / {total_count} activities to automate.")
            return selected
        else:
            print("[!] Invalid selection. Please enter valid numbers, 'all', 'pending', or 'q' to cancel.")


def prompt_activity_selection(
    activities: list[dict],
    course_name: str = "",
    tracker=None,
    headless: bool = False,
    force_cli: bool = False,
    auto_all: bool = False
) -> list[dict]:
    """
    Prompts the user to select which activities to automate.
    Attempts GUI Checkbox Dialog first, smoothly falling back to CLI selector.
    """
    if not activities:
        return []

    if auto_all:
        print(f"\n[Activity Selector] --all flag specified. Automatically selected all {len(activities)} activities.")
        return list(activities)

    # If headless or CLI forced, use CLI selector
    if headless or force_cli:
        return cli_activity_selector(activities, course_name=course_name, tracker=tracker)

    # Attempt Tkinter GUI Dialog
    try:
        dialog = ActivitySelectorDialog(activities, course_name=course_name, tracker=tracker)
        result = dialog.show()
        if result is not None:
            if not result:
                print("\n[Activity Selector] 0 activities selected in dialog. Skipping course activities.")
            else:
                print(f"\n[Activity Selector] Dialog selection confirmed: {len(result)} / {len(activities)} activities.")
            return result
        else:
            # User clicked cancel in dialog
            print("\n[Activity Selector] Selection cancelled by user in dialog.")
            return []
    except Exception as e:
        print(f"\n[Activity Selector] GUI dialog unavailable ({e}). Falling back to interactive CLI mode.")
        return cli_activity_selector(activities, course_name=course_name, tracker=tracker)
