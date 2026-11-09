"""Dashboard tab: customizable widgets for the home screen."""
import os
import sys
import time
import threading
import urllib.request
import tkinter as tk
from tkinter import ttk
from datetime import datetime, timedelta
from core.db import _get_tasks, _get_conversations, _db

# Optional Windows media keys
try:
    import ctypes
    VK_MEDIA_PLAY_PAUSE = 0xB3
    VK_MEDIA_NEXT_TRACK = 0xB0
    VK_MEDIA_PREV_TRACK = 0xB1
    VK_MEDIA_STOP = 0xB2
except ImportError:
    pass


class DashboardTab(ttk.Frame):
    # Minimum width of one widget column. The number of columns is
    # recalculated on every resize: floor(width / MIN_COL_WIDTH), capped 1..4.
    MIN_COL_WIDTH = 320
    MAX_COLS = 4

    def __init__(self, parent, app):
        super().__init__(parent, padding=12)
        self.app = app
        self.theme = app.theme

        # Widget toggles
        self.widget_vars = {
            "Context Gauge":    tk.BooleanVar(value=True),
            "Today's Agenda":   tk.BooleanVar(value=True),
            "Quick Tasks":      tk.BooleanVar(value=True),
            "Network Status":   tk.BooleanVar(value=True),
            "Recent Files":     tk.BooleanVar(value=True),
            "Weather":          tk.BooleanVar(value=True),
            "Media Controls":   tk.BooleanVar(value=(sys.platform == "win32")),
            "Clock":            tk.BooleanVar(value=True),
        }

        self._current_cols = 2
        self._canvas_window_id = None

        self._build_ui()
        self._update_clock()
        self._refresh_network()
        self._refresh_weather()

    # ----- layout -----------------------------------------------------

    def _build_ui(self):
        self.columnconfigure(1, weight=1)
        self.rowconfigure(0, weight=1)

        # --- Left sidebar (toggles) ---
        sidebar = ttk.Frame(self, padding=8, width=180)
        sidebar.grid(row=0, column=0, sticky="ns", padx=(0, 12))
        sidebar.grid_propagate(False)

        ttk.Label(sidebar, text="Workspace Widgets",
                  font=("Segoe UI", 11, "bold")).pack(anchor="w", pady=(0, 10))

        for name, var in self.widget_vars.items():
            ttk.Checkbutton(sidebar, text=name, variable=var,
                            command=self._refresh_widgets).pack(anchor="w", pady=2)

        ttk.Button(sidebar, text="Refresh Data",
                   command=self._load_data).pack(anchor="w", pady=(20, 0))

        # --- Right area: scrollable widget grid ---
        self.canvas = tk.Canvas(self, bg=self.app.theme["bg_main"],
                                highlightthickness=0)
        self.scrollbar = ttk.Scrollbar(self, orient="vertical",
                                       command=self.canvas.yview)
        self.scroll_frame = ttk.Frame(self.canvas)

        self._canvas_window_id = self.canvas.create_window(
            (0, 0), window=self.scroll_frame, anchor="nw"
        )
        self.canvas.configure(yscrollcommand=self.scrollbar.set)

        # Two key bindings: canvas Configure drives responsive sizing, frame
        # Configure keeps the scrollregion in sync with the content.
        self.canvas.bind("<Configure>", self._on_canvas_configure)
        self.scroll_frame.bind("<Configure>", self._on_inner_configure)

        self.canvas.grid(row=0, column=1, sticky="nsew")
        self.scrollbar.grid(row=0, column=2, sticky="ns")

        self.grid_frame = self.scroll_frame
        self.widgets = {}

        # 1. Clock — expand=True so the label centres vertically when the
        #    card is stretched by the responsive grid.
        self.clock_frame = ttk.Frame(self.grid_frame, relief="solid", borderwidth=1)
        self.clock_label = ttk.Label(self.clock_frame, text="",
                                     font=("Consolas", 24, "bold"))
        self.clock_label.pack(expand=True, pady=20)
        self.widgets["Clock"] = self.clock_frame

        # 2. Context Gauge
        self.ctx_frame = ttk.Frame(self.grid_frame, relief="solid", borderwidth=1)
        ttk.Label(self.ctx_frame, text="📊 Context Window",
                  font=("Segoe UI", 10, "bold")).pack(anchor="w", padx=10, pady=(10, 5))
        self.ctx_bar = ttk.Progressbar(self.ctx_frame, orient="horizontal",
                                       mode="determinate")
        self.ctx_bar.pack(fill="x", padx=10, pady=5)
        self.ctx_label = ttk.Label(self.ctx_frame, text="0 / 0 tokens",
                                   font=("Consolas", 9))
        self.ctx_label.pack(anchor="w", padx=10, pady=(0, 10))
        self.widgets["Context Gauge"] = self.ctx_frame

        # 3. Today's Agenda
        self.agenda_frame = ttk.Frame(self.grid_frame, relief="solid", borderwidth=1)
        ttk.Label(self.agenda_frame, text="📅 Today's Agenda",
                  font=("Segoe UI", 10, "bold")).pack(anchor="w", padx=10, pady=(10, 5))
        self.agenda_list = tk.Listbox(self.agenda_frame, height=5,
                                      font=("Segoe UI", 9),
                                      bg=self.app.theme["bg_panel"],
                                      fg=self.app.theme["fg_main"], borderwidth=0)
        self.agenda_list.pack(fill="both", expand=True, padx=10, pady=(0, 10))
        self.widgets["Today's Agenda"] = self.agenda_frame

        # 4. Quick Tasks
        self.tasks_frame = ttk.Frame(self.grid_frame, relief="solid", borderwidth=1)
        ttk.Label(self.tasks_frame, text="📋 Pending Tasks",
                  font=("Segoe UI", 10, "bold")).pack(anchor="w", padx=10, pady=(10, 5))
        self.tasks_list = tk.Listbox(self.tasks_frame, height=5,
                                     font=("Segoe UI", 9),
                                     bg=self.app.theme["bg_panel"],
                                     fg=self.app.theme["fg_main"], borderwidth=0)
        self.tasks_list.pack(fill="both", expand=True, padx=10, pady=(0, 10))
        self.widgets["Quick Tasks"] = self.tasks_frame

        # 5. Network Status
        self.net_frame = ttk.Frame(self.grid_frame, relief="solid", borderwidth=1)
        ttk.Label(self.net_frame, text="🌐 Network Status",
                  font=("Segoe UI", 10, "bold")).pack(anchor="w", padx=10, pady=(10, 5))
        self.net_status_label = ttk.Label(self.net_frame, text="🔴 Offline",
                                          font=("Segoe UI", 11, "bold"))
        self.net_status_label.pack(expand=True, padx=10, pady=5)
        self.net_ping_label = ttk.Label(self.net_frame, text="Ping: --",
                                        font=("Consolas", 9))
        self.net_ping_label.pack(anchor="w", padx=10, pady=(0, 10))
        self.widgets["Network Status"] = self.net_frame

        # 6. Recent Files
        self.files_frame = ttk.Frame(self.grid_frame, relief="solid", borderwidth=1)
        ttk.Label(self.files_frame, text="📂 Recent Batch Files",
                  font=("Segoe UI", 10, "bold")).pack(anchor="w", padx=10, pady=(10, 5))
        self.files_list = tk.Listbox(self.files_frame, height=5,
                                     font=("Consolas", 9),
                                     bg=self.app.theme["bg_panel"],
                                     fg=self.app.theme["fg_main"], borderwidth=0)
        self.files_list.pack(fill="both", expand=True, padx=10, pady=(0, 5))
        self.files_list.bind("<Double-Button-1>", self._open_recent_file)
        ttk.Label(self.files_frame, text="Double-click to open",
                  font=("Segoe UI", 8, "italic"),
                  foreground=self.app.theme["fg_muted"]).pack(
                      anchor="w", padx=10, pady=(0, 10))
        self.widgets["Recent Files"] = self.files_frame

        # 7. Weather
        self.weather_frame = ttk.Frame(self.grid_frame, relief="solid", borderwidth=1)
        ttk.Label(self.weather_frame, text="🌤️ Weather",
                  font=("Segoe UI", 10, "bold")).pack(anchor="w", padx=10, pady=(10, 5))
        self.weather_label = ttk.Label(self.weather_frame, text="Fetching...",
                                       font=("Segoe UI", 11))
        self.weather_label.pack(expand=True, padx=10, pady=5)
        ttk.Button(self.weather_frame, text="Refresh",
                   command=self._refresh_weather).pack(anchor="w", padx=10, pady=(0, 10))
        self.widgets["Weather"] = self.weather_frame

        # 8. Media Controls
        self.media_frame = ttk.Frame(self.grid_frame, relief="solid", borderwidth=1)
        ttk.Label(self.media_frame, text="🎵 Media Controls",
                  font=("Segoe UI", 10, "bold")).pack(anchor="w", padx=10, pady=(10, 5))
        btn_row = ttk.Frame(self.media_frame)
        btn_row.pack(expand=True, pady=10)
        ttk.Button(btn_row, text="⏮", width=4,
                   command=lambda: self._send_media_key(VK_MEDIA_PREV_TRACK)).pack(side=tk.LEFT, padx=2)
        ttk.Button(btn_row, text="⏯", width=4,
                   command=lambda: self._send_media_key(VK_MEDIA_PLAY_PAUSE)).pack(side=tk.LEFT, padx=2)
        ttk.Button(btn_row, text="⏭", width=4,
                   command=lambda: self._send_media_key(VK_MEDIA_NEXT_TRACK)).pack(side=tk.LEFT, padx=2)
        ttk.Button(btn_row, text="⏹", width=4,
                   command=lambda: self._send_media_key(VK_MEDIA_STOP)).pack(side=tk.LEFT, padx=2)
        self.widgets["Media Controls"] = self.media_frame

        self._layout_widgets()
        self._load_data()
        self.after(5000, self._auto_refresh_loop)

    # ----- responsive layout ------------------------------------------

    def _target_columns(self, width):
        """Pick a column count based on available width (1..MAX_COLS)."""
        return min(self.MAX_COLS, max(1, width // self.MIN_COL_WIDTH))

    def _on_canvas_configure(self, event):
        if event.width <= 1 or event.height <= 1:
            return
        target = self._target_columns(event.width)
        if target != self._current_cols:
            self._current_cols = target
            self._layout_widgets()   # ends with _refit_canvas()
        else:
            self._refit_canvas()

    def _on_inner_configure(self, event=None):
        try:
            self.canvas.configure(scrollregion=self.canvas.bbox("all"))
        except Exception:
            pass

    def _layout_widgets(self):
        """Re-grid all visible widgets with dynamic row/column weights.

        Every visible row and column gets weight=1 so widgets expand to fill
        the available space; the uniform="dashcol/dashrow" hints keep cells
        the same size within each axis.
        """
        for w in self.widgets.values():
            w.grid_forget()

        # Clear previous weights
        for i in range(32):
            self.grid_frame.rowconfigure(i, weight=0)
        for i in range(12):
            self.grid_frame.columnconfigure(i, weight=0)

        num_cols = max(1, self._current_cols)
        for i in range(num_cols):
            self.grid_frame.columnconfigure(i, weight=1, uniform="dashcol")

        visible = [name for name, var in self.widget_vars.items() if var.get()]
        if not visible:
            self._refit_canvas()
            return

        num_rows = (len(visible) + num_cols - 1) // num_cols
        for i in range(num_rows):
            self.grid_frame.rowconfigure(i, weight=1, uniform="dashrow")

        row = col = 0
        for name in visible:
            self.widgets[name].grid(
                row=row, column=col, sticky="nsew", padx=5, pady=5
            )
            col += 1
            if col >= num_cols:
                col = 0
                row += 1

        self._refit_canvas()

    def _refit_canvas(self):
        """Size the inner frame to fill the canvas when content is smaller.

        If content is taller than the canvas, use its natural height and
        show the scrollbar; otherwise stretch to canvas height and hide it.
        """
        w = self.canvas.winfo_width()
        h = self.canvas.winfo_height()
        if w <= 1 or h <= 1:
            return
        self.scroll_frame.update_idletasks()
        req_h = self.scroll_frame.winfo_reqheight()
        new_h = max(req_h, h)
        try:
            self.canvas.itemconfigure(
                self._canvas_window_id, width=w, height=new_h
            )
            self.canvas.configure(scrollregion=(0, 0, w, new_h))
        except Exception:
            return

        if new_h > h + 1:
            self.scrollbar.grid(row=0, column=2, sticky="ns")
        else:
            self.scrollbar.grid_remove()

    def _refresh_widgets(self):
        """Called when a widget checkbox is toggled: re-grid + reload data."""
        self._layout_widgets()
        self._load_data()

    # ----- refresh loops ----------------------------------------------

    def _update_clock(self):
        now = datetime.now().strftime("%H:%M:%S")
        try:
            self.clock_label.config(text=now)
        except Exception:
            pass
        self.after(1000, self._update_clock)

    def _auto_refresh_loop(self):
        self._load_data()
        self._refresh_network()
        self.after(30000, self._auto_refresh_loop)

    # ----- data loading -----------------------------------------------

    def _load_data(self):
        # 1. Context Gauge
        if self.widget_vars["Context Gauge"].get():
            try:
                from core.db import _get_messages
                msgs = (_get_messages(self.app.current_conversation_id)
                        if self.app.current_conversation_id else [])
                total_chars = sum(len(m["content"]) for m in msgs)
                approx_tokens = total_chars // 4
                limit = self.app.app_config.get("num_ctx", 16384)

                self.ctx_bar["maximum"] = limit
                self.ctx_bar["value"] = min(approx_tokens, limit)

                pct = (approx_tokens / limit) * 100 if limit else 0
                self.ctx_label.config(
                    text=f"{approx_tokens} / {limit} tokens ({pct:.1f}%)")

                self.ctx_bar.configure(
                    style="red.Horizontal.TProgressbar" if pct > 80
                    else "Horizontal.TProgressbar"
                )
            except Exception as e:
                print(f"Ctx gauge error: {e}")

        # 2. Today's Agenda
        if self.widget_vars["Today's Agenda"].get():
            self.agenda_list.delete(0, tk.END)
            try:
                today_start = datetime.now().replace(
                    hour=0, minute=0, second=0, microsecond=0)
                today_end = today_start + timedelta(days=1)
                with _db() as conn:
                    events = conn.execute(
                        "SELECT title, start_ts FROM events WHERE start_ts >= ? "
                        "AND start_ts < ? ORDER BY start_ts",
                        (today_start.strftime("%Y-%m-%dT%H:%M"),
                         today_end.strftime("%Y-%m-%dT%H:%M"))
                    ).fetchall()
                for ev in events:
                    time_str = ev["start_ts"].split("T")[1]
                    self.agenda_list.insert(tk.END, f"{time_str} - {ev['title']}")
                if not events:
                    self.agenda_list.insert(tk.END, "No events today.")
            except Exception as e:
                print(f"Agenda error: {e}")

        # 3. Quick Tasks
        if self.widget_vars["Quick Tasks"].get():
            self.tasks_list.delete(0, tk.END)
            try:
                for t in _get_tasks()[:5]:
                    self.tasks_list.insert(tk.END, f"[{t['priority'][0].upper()}] {t['title']}")
            except Exception:
                pass

        # 4. Recent Files
        if self.widget_vars["Recent Files"].get():
            self.files_list.delete(0, tk.END)
            recent = self.app.app_config.get("recent_batch_files", [])
            for f in recent:
                self.files_list.insert(tk.END, f)
            if not recent:
                self.files_list.insert(tk.END, "No recent files.")

    # ----- network / weather ------------------------------------------

    def _refresh_network(self):
        if not self.widget_vars["Network Status"].get():
            return

        def _worker():
            url = self.app.app_config.get("ollama_url",
                                          "http://localhost:11434/api/chat")
            base_url = url.split("/api/")[0]
            start = time.perf_counter()
            try:
                with urllib.request.urlopen(base_url, timeout=3):
                    latency = int((time.perf_counter() - start) * 1000)
                    self.app.ui_queue.put(("dashboard_net", (True, latency)))
            except Exception:
                self.app.ui_queue.put(("dashboard_net", (False, 0)))

        threading.Thread(target=_worker, daemon=True).start()

    def _refresh_weather(self):
        if not self.widget_vars["Weather"].get():
            return

        def _worker():
            try:
                req = urllib.request.Request(
                    "https://wttr.in/?format=%l:+%c+%t",
                    headers={"User-Agent": "curl/7.68.0"},
                )
                with urllib.request.urlopen(req, timeout=5) as resp:
                    weather_text = resp.read().decode("utf-8").strip()
                self.app.ui_queue.put(("dashboard_weather", weather_text))
            except Exception:
                self.app.ui_queue.put(("dashboard_weather", "Weather offline."))

        threading.Thread(target=_worker, daemon=True).start()

    # ----- misc handlers ----------------------------------------------

    def _open_recent_file(self, event):
        sel = self.files_list.curselection()
        if not sel:
            return
        filepath = self.files_list.get(sel[0])
        if filepath == "No recent files.":
            return
        if not os.path.exists(filepath):
            tk.messagebox.showerror("File not found", f"Could not find:\n{filepath}")
            return
        try:
            if sys.platform == "win32":
                os.startfile(filepath)
            elif sys.platform == "darwin":
                import subprocess
                subprocess.Popen(["open", filepath])
            else:
                import subprocess
                subprocess.Popen(["xdg-open", filepath])
        except Exception as e:
            tk.messagebox.showerror("Error opening file", str(e))

    def _send_media_key(self, vk_code):
        if sys.platform != "win32":
            return
        try:
            ctypes.windll.user32.keybd_event(vk_code, 0, 0, 0)
            ctypes.windll.user32.keybd_event(vk_code, 0, 2, 0)
        except Exception as e:
            print(f"Media key error: {e}")

    def handle_ui_event(self, kind, payload):
        if kind == "dashboard_net":
            online, latency = payload
            if online:
                self.net_status_label.config(
                    text="🟢 Online", foreground=self.app.theme["accent_teal"])
                self.net_ping_label.config(text=f"Ping: {latency} ms")
            else:
                self.net_status_label.config(
                    text="🔴 Offline", foreground=self.app.theme["accent_red"])
                self.net_ping_label.config(text="Ping: --")
        elif kind == "dashboard_weather":
            self.weather_label.config(text=payload)