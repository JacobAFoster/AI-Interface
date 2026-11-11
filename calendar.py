"""Calendar tab: month grid + day detail panel."""
import re
import tkinter as tk
from tkinter import ttk, messagebox
from datetime import datetime, timedelta

from core.db import _db, DEFAULT_REMINDER_MINUTES
from core.parsing import _parse_ts_strict, _parse_recurrence


class CalendarTab(ttk.Frame):
    def __init__(self, parent, app):
        super().__init__(parent, padding=12)
        self.app = app
        self.current_month = datetime.now().replace(day=1)
        self.selected_date = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        self.events_cache = []
        self._build_ui()
        self.refresh()

    def _build_ui(self):
        toolbar = ttk.Frame(self)
        toolbar.pack(fill="x", pady=(0, 8))
        ttk.Button(toolbar, text="◀", width=3, command=self._prev_month).pack(side=tk.LEFT)
        self.title_label = ttk.Label(toolbar, text="", font=("Segoe UI", 13, "bold"))
        self.title_label.pack(side=tk.LEFT, padx=8)
        ttk.Button(toolbar, text="▶", width=3, command=self._next_month).pack(side=tk.LEFT)
        ttk.Button(toolbar, text="Today", command=self._goto_today).pack(side=tk.LEFT, padx=(8, 0))
        ttk.Button(toolbar, text="+ New Event",
                   command=self._open_event_dialog).pack(side=tk.LEFT, padx=(8, 0))
        ttk.Button(toolbar, text="🔄 Refresh",
                   command=self._manual_refresh).pack(side=tk.LEFT, padx=(8, 0))

        main = ttk.Frame(self)
        main.pack(fill="both", expand=True)
        self.grid_frame = tk.Frame(main, bg=self.app.theme["border"])
        self.grid_frame.pack(side=tk.LEFT, fill="both", expand=True)
        for i in range(7):
            self.grid_frame.columnconfigure(i, weight=1, uniform="cal")
        for i in range(7):
            self.grid_frame.rowconfigure(i, weight=(0 if i == 0 else 1), uniform="calrow")

        side = tk.Frame(main, bg=self.app.theme["bg_panel"], width=290)
        side.pack(side=tk.RIGHT, fill="y", padx=(8, 0))
        side.pack_propagate(False)
        self.side_date_label = tk.Label(side, text="", bg=self.app.theme["bg_panel"], fg=self.app.theme["accent_teal"], font=("Segoe UI", 11, "bold"), anchor="w")
        self.side_date_label.pack(fill="x", padx=10, pady=(10, 6))
        canvas = tk.Canvas(side, bg=self.app.theme["bg_panel"], highlightthickness=0)
        scrollbar = ttk.Scrollbar(side, orient="vertical", command=canvas.yview)
        self.side_inner = tk.Frame(canvas, bg=self.app.theme["bg_panel"])
        self.side_inner.bind("<Configure>",
                             lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.create_window((0, 0), window=self.side_inner, anchor="nw")
        canvas.configure(yscrollcommand=scrollbar.set)
        canvas.pack(side=tk.LEFT, fill="both", expand=True, padx=(10, 0), pady=(0, 10))
        scrollbar.pack(side=tk.RIGHT, fill="y", pady=(0, 10), padx=(0, 6))

        self._bind_side_mousewheel(side, canvas)

    def _bind_side_mousewheel(self, region, canvas):
        def _handler(e):
            w = e.widget
            try:
                while w is not None:
                    if w is region:
                        break
                    w = w.master
                if w is None:
                    return
            except Exception:
                return
            if getattr(e, "num", None) == 4:
                canvas.yview_scroll(-1, "units")
            elif getattr(e, "num", None) == 5:
                canvas.yview_scroll(1, "units")
            else:
                delta = getattr(e, "delta", 0)
                canvas.yview_scroll(int(-1 * (delta / 120)), "units")

        self.bind_all("<MouseWheel>", _handler, add="+")
        self.bind_all("<Button-4>", _handler, add="+")
        self.bind_all("<Button-5>", _handler, add="+")

    def _prev_month(self):
        self.current_month = (self.current_month - timedelta(days=1)).replace(day=1)
        self.refresh()

    def _next_month(self):
        if self.current_month.month == 12:
            self.current_month = self.current_month.replace(
                year=self.current_month.year + 1, month=1)
        else:
            self.current_month = self.current_month.replace(
                month=self.current_month.month + 1)
        self.refresh()

    def _goto_today(self):
        now = datetime.now()
        self.current_month = now.replace(day=1)
        self.selected_date = now.replace(hour=0, minute=0, second=0, microsecond=0)
        self.refresh()

    def _manual_refresh(self):
        self.refresh()
        self.app.flash_status("Status: Calendar refreshed", "green")

    def _select_date(self, d):
        self.selected_date = d.replace(hour=0, minute=0, second=0, microsecond=0)
        self._render_side()

    def refresh(self):
        y, m = self.current_month.year, self.current_month.month
        first = datetime(y, m, 1)
        grid_start = first - timedelta(days=(first.weekday() + 1) % 7)
        grid_end = grid_start + timedelta(days=42)
        with _db() as conn:
            rows = conn.execute(
                "SELECT * FROM events WHERE start_ts < ? AND end_ts > ? ORDER BY start_ts",
                (grid_end.strftime("%Y-%m-%dT%H:%M"),
                 grid_start.strftime("%Y-%m-%dT%H:%M"))
            ).fetchall()
            self.events_cache = [dict(r) for r in rows]
        self._render()

    def _events_for_day(self, d):
        day_start = d.replace(hour=0, minute=0, second=0, microsecond=0)
        day_end = day_start + timedelta(days=1)
        ds = day_start.strftime("%Y-%m-%dT%H:%M")
        de = day_end.strftime("%Y-%m-%dT%H:%M")
        return [e for e in self.events_cache if e["start_ts"] < de and e["end_ts"] > ds]

    def _render(self):
        self.title_label.config(text=self.current_month.strftime("%B %Y"))
        for w in self.grid_frame.winfo_children():
            w.destroy()
        for i, day_name in enumerate(["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"]):
            tk.Label(self.grid_frame, text=day_name, bg=self.app.theme["bg_panel"], fg=self.app.theme["fg_muted"],
                     font=("Segoe UI", 8, "bold")).grid(
                         row=0, column=i, sticky="nsew", padx=1, pady=1)

        y, m = self.current_month.year, self.current_month.month
        first = datetime(y, m, 1)
        grid_start = first - timedelta(days=(first.weekday() + 1) % 7)
        today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)

        for i in range(42):
            d = grid_start + timedelta(days=i)
            row, col = (i // 7) + 1, i % 7
            is_other = d.month != m
            is_today = d.date() == today.date()
            is_sel = d.date() == self.selected_date.date()

            bg = self.app.theme["bg_card"] if is_other else self.app.theme["bg_main"]
            border_color = self.app.theme["accent_blue"] if is_sel else self.app.theme["border"]
            cell = tk.Frame(self.grid_frame, bg=bg,
                            highlightthickness=2 if is_sel else 1,
                            highlightbackground=border_color, highlightcolor=border_color)
            cell.grid(row=row, column=col, sticky="nsew", padx=1, pady=1)

            num_bg = self.app.theme["accent_blue"] if is_today else bg
            num_fg = "#fff" if is_today else (self.app.theme["fg_muted"] if is_other else self.app.theme["fg_main"])
            tk.Label(cell, text=str(d.day), bg=num_bg, fg=num_fg,
                     font=("Segoe UI", 9, "bold"), anchor="w").pack(
                         fill="x", padx=4, pady=(3, 2))

            evs = self._events_for_day(d)
            for ev in evs[:2]:
                tk.Label(cell, text=ev["title"][:14], bg=self.app.theme["accent_blue"], fg="white",
                         font=("Segoe UI", 7), anchor="w").pack(fill="x", padx=2)
            if len(evs) > 2:
                tk.Label(cell, text=f"+{len(evs) - 2} more", bg=bg, fg=self.app.theme["fg_muted"],
                         font=("Segoe UI", 7), anchor="w").pack(fill="x", padx=4)

            for w in [cell] + list(cell.winfo_children()):
                w.bind("<Button-1>", lambda e, dt=d: self._select_date(dt))
        self._render_side()

    def _render_side(self):
        self.side_date_label.config(text=self.selected_date.strftime("%A, %B %d, %Y"))
        for w in self.side_inner.winfo_children():
            w.destroy()
        events = self._events_for_day(self.selected_date)
        if not events:
            tk.Label(self.side_inner, text="No events", bg=self.app.theme["bg_panel"], fg=self.app.theme["fg_muted"],
                     font=("Segoe UI", 9, "italic")).pack(anchor="w", pady=8)
            return
        for ev in events:
            card = tk.Frame(self.side_inner, bg=self.app.theme["bg_main"],
                            highlightthickness=1, highlightbackground=self.app.theme["border"])
            card.pack(fill="x", pady=4)
            try:
                s = datetime.strptime(ev["start_ts"], "%Y-%m-%dT%H:%M")
                en = datetime.strptime(ev["end_ts"], "%Y-%m-%dT%H:%M")
                tstr = f"{s.strftime('%H:%M')} – {en.strftime('%H:%M')}"
            except Exception:
                tstr = f"{ev['start_ts']} → {ev['end_ts']}"
            tk.Label(card, text=tstr, bg=self.app.theme["bg_main"], fg=self.app.theme["fg_muted"],
                     font=("Consolas", 8), anchor="w").pack(
                         fill="x", padx=8, pady=(6, 0))
            tk.Label(card, text=ev["title"], bg=self.app.theme["bg_main"], fg=self.app.theme["fg_main"],
                     font=("Segoe UI", 9, "bold"), anchor="w",
                     wraplength=250, justify="left").pack(fill="x", padx=8)
            rm = ev.get("reminder_minutes")
            if rm is not None:
                rm_txt = f"🔔 {rm} min before" if rm > 0 else "🔕 reminders off"
                rm_color = self.app.theme["accent_teal"] if rm > 0 else self.app.theme["fg_muted"]
                tk.Label(card, text=rm_txt, bg=self.app.theme["bg_main"], fg=rm_color,
                         font=("Segoe UI", 8), anchor="w").pack(
                             fill="x", padx=8, pady=(2, 0))
            if ev.get("description"):
                tk.Label(card, text=ev["description"], bg=self.app.theme["bg_main"], fg=self.app.theme["fg_muted"],
                         font=("Segoe UI", 8), anchor="w",
                         wraplength=250, justify="left").pack(
                             fill="x", padx=8, pady=(2, 0))
            btn_row = tk.Frame(card, bg=self.app.theme["bg_main"])
            btn_row.pack(fill="x", padx=8, pady=(4, 6))
            ttk.Button(btn_row, text="Edit", width=6,
                       command=lambda e=ev: self._open_event_dialog(e)).pack(side=tk.LEFT)
            ttk.Button(btn_row, text="Delete", width=6,
                       command=lambda e=ev: self._delete_event(e)).pack(
                           side=tk.LEFT, padx=(4, 0))

    def _delete_event(self, ev):
        if not messagebox.askyesno("Delete Event", f"Delete '{ev['title']}'?"):
            return
        try:
            with _db() as conn:
                conn.execute("DELETE FROM events WHERE id=?", (ev["id"],))
            self.refresh()
            self.app.flash_status("Status: Event deleted", "green")
        except Exception as e:
            self.app.flash_status(f"Error: {e}", "red")

    def _open_event_dialog(self, event=None):
        dlg = tk.Toplevel(self)
        dlg.title("Edit Event" if event else "New Event")
        dlg.transient(self.winfo_toplevel())
        dlg.grab_set()
        dlg.resizable(False, False)
        pad = {"padx": 10, "pady": 4}

        row1 = ttk.Frame(dlg); row1.pack(fill="x", **pad)
        ttk.Label(row1, text="Title:", width=10).pack(side=tk.LEFT)
        title_var = tk.StringVar(value=event["title"] if event else "")
        ttk.Entry(row1, textvariable=title_var, width=42).pack(side=tk.LEFT)

        row2 = ttk.Frame(dlg); row2.pack(fill="x", **pad)
        ttk.Label(row2, text="Start:", width=10).pack(side=tk.LEFT)
        sv = (datetime.strptime(event["start_ts"], "%Y-%m-%dT%H:%M").strftime("%Y-%m-%d %H:%M")
              if event else self.selected_date.replace(hour=9, minute=0).strftime("%Y-%m-%d %H:%M"))
        start_var = tk.StringVar(value=sv)
        ttk.Entry(row2, textvariable=start_var, width=20).pack(side=tk.LEFT)
        ttk.Label(row2, text="  (YYYY-MM-DD HH:MM)", foreground="gray").pack(side=tk.LEFT)

        row3 = ttk.Frame(dlg); row3.pack(fill="x", **pad)
        ttk.Label(row3, text="End:", width=10).pack(side=tk.LEFT)
        ev_str = (datetime.strptime(event["end_ts"], "%Y-%m-%dT%H:%M").strftime("%Y-%m-%d %H:%M")
                  if event else self.selected_date.replace(hour=10, minute=0).strftime("%Y-%m-%d %H:%M"))
        end_var = tk.StringVar(value=ev_str)
        ttk.Entry(row3, textvariable=end_var, width=20).pack(side=tk.LEFT)

        row3b = ttk.Frame(dlg); row3b.pack(fill="x", **pad)
        ttk.Label(row3b, text="Remind:", width=10).pack(side=tk.LEFT)
        if event is not None and event.get("reminder_minutes") is not None:
            rm_val = str(event.get("reminder_minutes"))
        else:
            rm_val = str(self.app.app_config.get("default_reminder_minutes", 30))
        reminder_var = tk.StringVar(value=rm_val)
        ttk.Entry(row3b, textvariable=reminder_var, width=10).pack(side=tk.LEFT)
        ttk.Label(row3b, text="  minutes before start (0 = off)",
                  foreground="gray").pack(side=tk.LEFT)

        row3c = ttk.Frame(dlg); row3c.pack(fill="x", **pad)
        ttk.Label(row3c, text="Repeats:", width=10).pack(side=tk.LEFT)
        rec_var = tk.StringVar(value=((event.get("recurrence") or "none") if event else "none"))
        ttk.Combobox(row3c, textvariable=rec_var,
                     values=["none", "daily", "weekly", "monthly"],
                     state="readonly", width=10).pack(side=tk.LEFT)

        row4 = ttk.Frame(dlg); row4.pack(fill="x", **pad)
        ttk.Label(row4, text="Description:").pack(anchor="w")
        desc_text = tk.Text(dlg, height=3, width=55, wrap=tk.WORD, bg=self.app.theme["bg_panel"], fg=self.app.theme["fg_main"])
        desc_text.pack(fill="x", padx=10, pady=(0, 6))
        if event:
            desc_text.insert("1.0", event.get("description", ""))

        def _save():
            try:
                t = title_var.get().strip()
                s_raw = start_var.get().strip()
                e_raw = end_var.get().strip()
                if not t:
                    raise ValueError("Title is required")

                s = _parse_ts_strict(s_raw)
                en = _parse_ts_strict(e_raw)
                if not s:
                    raise ValueError("Start must be a valid date/time (YYYY-MM-DD HH:MM)")
                if not en:
                    raise ValueError("End must be a valid date/time (YYYY-MM-DD HH:MM)")
                if en <= s:
                    raise ValueError("End must be after start")

                rm_raw = reminder_var.get().strip()
                try:
                    rm_int = int(rm_raw)
                except ValueError:
                    raise ValueError("Remind must be a whole number of minutes")
                if rm_int < 0 or rm_int > 10080:
                    raise ValueError("Remind must be between 0 and 10080 minutes")

                d = desc_text.get("1.0", "end-1c").strip()
                rec = _parse_recurrence(rec_var.get())
                with _db() as conn:
                    if event:
                        conn.execute(
                            "UPDATE events SET title=?, start_ts=?, end_ts=?, "
                            "description=?, reminder_minutes=?, recurrence=? WHERE id=?",
                            (t, s, en, d, rm_int, rec, event["id"]))
                    else:
                        conn.execute(
                            "INSERT INTO events (title, start_ts, end_ts, description, "
                            "reminder_minutes, recurrence) VALUES (?, ?, ?, ?, ?, ?)",
                            (t, s, en, d, rm_int, rec))
                if event:
                    svc = getattr(self.app, "reminder_service", None)
                    if svc is not None:
                        svc.clear_notified_for("event", event["id"])
                dlg.destroy()
                self.refresh()
                self.app.flash_status("Status: Event saved", "green")
            except Exception as ex:
                messagebox.showerror("Error", str(ex), parent=dlg)

        btn_row = ttk.Frame(dlg); btn_row.pack(fill="x", padx=10, pady=(4, 10))
        ttk.Button(btn_row, text="Cancel", command=dlg.destroy).pack(side=tk.RIGHT)
        ttk.Button(btn_row, text="Save", command=_save).pack(side=tk.RIGHT, padx=(0, 6))