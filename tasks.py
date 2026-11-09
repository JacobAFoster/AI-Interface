"""Tasks tab: list of to-dos with optional due date and priority."""
import tkinter as tk
from tkinter import ttk, messagebox

from core.db import (
    DEFAULT_REMINDER_MINUTES,
    _get_tasks, _add_task, _update_task, _toggle_task, _delete_task,
)
from core.parsing import _parse_ts_strict


class TaskTab(ttk.Frame):
    def __init__(self, parent, app):
        super().__init__(parent, padding=12)
        self.app = app
        self.show_done_var = tk.BooleanVar(value=False)
        self._build_ui()
        self.refresh()

    def _build_ui(self):
        toolbar = ttk.Frame(self)
        toolbar.pack(fill="x", pady=(0, 8))
        ttk.Label(toolbar, text="📋 Tasks",
                  font=("Segoe UI", 13, "bold")).pack(side=tk.LEFT)
        ttk.Button(toolbar, text="+ New Task",
                   command=self._open_task_dialog).pack(side=tk.LEFT, padx=(8, 0))
        ttk.Button(toolbar, text="🔄 Refresh",
                   command=self.refresh).pack(side=tk.LEFT, padx=(8, 0))
        ttk.Checkbutton(toolbar, text="Show completed",
                        variable=self.show_done_var,
                        command=self.refresh).pack(side=tk.RIGHT)

        container = ttk.Frame(self)
        container.pack(fill="both", expand=True)
        self._canvas = tk.Canvas(container, bg="#1e1e1e", highlightthickness=0)
        scrollbar = ttk.Scrollbar(container, orient="vertical",
                                  command=self._canvas.yview)
        self.inner = tk.Frame(self._canvas, bg="#1e1e1e")
        self.inner.bind("<Configure>", lambda e: self._canvas.configure(
            scrollregion=self._canvas.bbox("all")))
        self._win = self._canvas.create_window((0, 0), window=self.inner, anchor="nw")
        self._canvas.bind("<Configure>", lambda e: self._canvas.itemconfig(
            self._win, width=e.width) if self._canvas.find_all() else None)
        self._canvas.configure(yscrollcommand=scrollbar.set)
        self._canvas.pack(side=tk.LEFT, fill="both", expand=True)
        scrollbar.pack(side=tk.RIGHT, fill="y")

        self._bind_mousewheel(container, self._canvas)

    def _bind_mousewheel(self, region, canvas):
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

    # --- Render -----------------------------------------------------------
    def refresh(self):
        for w in self.inner.winfo_children():
            w.destroy()
        tasks = _get_tasks(include_done=self.show_done_var.get())
        if not tasks:
            tk.Label(self.inner,
                     text="No tasks. Click '+ New Task', or ask the AI to add one.",
                     bg="#1e1e1e", fg="#666",
                     font=("Segoe UI", 10, "italic")).pack(pady=24)
            return
        for t in tasks:
            self._render_task(t)

    def _render_task(self, t):
        card = tk.Frame(self.inner, bg="#252526", highlightthickness=1,
                        highlightbackground="#333")
        card.pack(fill="x", padx=8, pady=4)

        left = tk.Frame(card, bg="#252526")
        left.pack(side=tk.LEFT, fill="both", expand=True, padx=(10, 8), pady=8)

        done_var = tk.BooleanVar(value=bool(t["done"]))
        ttk.Checkbutton(left, variable=done_var,
                        command=lambda tid=t["id"]: self._toggle(tid)
                        ).pack(side=tk.LEFT, padx=(0, 8), anchor="n")

        body = tk.Frame(left, bg="#252526")
        body.pack(side=tk.LEFT, fill="x", expand=True, anchor="w")

        title_font = ("Segoe UI", 10, "overstrike" if t["done"] else "bold")
        title_fg = "#666" if t["done"] else "#d4d4d4"
        tk.Label(body, text=t["title"], bg="#252526", fg=title_fg,
                 font=title_font, anchor="w").pack(anchor="w")

        prio_colors = {"high": "#f48771", "normal": "#4ec9b0", "low": "#888"}
        info = []
        if t.get("priority") and t["priority"] != "normal":
            info.append(f"priority: {t['priority']}")
        if t.get("due_ts"):
            info.append(f"due: {t['due_ts']}")
        if info:
            tk.Label(body, text="  ·  ".join(info), bg="#252526",
                     fg=prio_colors.get(t.get("priority", "normal"), "#4ec9b0"),
                     font=("Segoe UI", 8), anchor="w").pack(anchor="w", pady=(2, 0))
        rm = t.get("reminder_minutes")
        if rm is not None:
            rm_txt = f"🔔 {rm} min before due" if rm > 0 else "🔕 reminders off"
            rm_color = "#4ec9b0" if rm > 0 else "#888"
            tk.Label(body, text=rm_txt, bg="#252526", fg=rm_color,
                     font=("Segoe UI", 8), anchor="w").pack(anchor="w", pady=(1, 0))
        if t.get("description"):
            tk.Label(body, text=t["description"], bg="#252526", fg="#aaa",
                     font=("Segoe UI", 9), anchor="w", wraplength=620,
                     justify="left").pack(anchor="w", pady=(2, 0))

        btns = tk.Frame(card, bg="#252526")
        btns.pack(side=tk.RIGHT, padx=8, pady=8)
        ttk.Button(btns, text="Edit", width=6,
                   command=lambda task=t: self._open_task_dialog(task)
                   ).pack(side=tk.LEFT)
        ttk.Button(btns, text="Delete", width=7,
                   command=lambda task=t: self._delete(task)
                   ).pack(side=tk.LEFT, padx=(4, 0))

    # --- CRUD -------------------------------------------------------------
    def _toggle(self, task_id):
        try:
            _toggle_task(task_id)
            self.refresh()
        except Exception as e:
            self.app.flash_status(f"Error: {e}", "red")

    def _delete(self, t):
        if not messagebox.askyesno("Delete Task", f"Delete '{t['title']}'?"):
            return
        try:
            _delete_task(t["id"])
            self.refresh()
            self.app.flash_status("Status: Task deleted", "green")
        except Exception as e:
            self.app.flash_status(f"Error: {e}", "red")

    def _open_task_dialog(self, task=None):
        dlg = tk.Toplevel(self)
        dlg.title("Edit Task" if task else "New Task")
        dlg.transient(self.winfo_toplevel())
        dlg.grab_set()
        dlg.resizable(False, False)
        pad = {"padx": 10, "pady": 4}

        row1 = ttk.Frame(dlg); row1.pack(fill="x", **pad)
        ttk.Label(row1, text="Title:", width=10).pack(side=tk.LEFT)
        title_var = tk.StringVar(value=task["title"] if task else "")
        ttk.Entry(row1, textvariable=title_var, width=48).pack(side=tk.LEFT)

        row2 = ttk.Frame(dlg); row2.pack(fill="x", **pad)
        ttk.Label(row2, text="Due:", width=10).pack(side=tk.LEFT)
        due_var = tk.StringVar(
            value=(task["due_ts"] if task and task.get("due_ts") else ""))
        ttk.Entry(row2, textvariable=due_var, width=24).pack(side=tk.LEFT)
        ttk.Label(row2, text="  (YYYY-MM-DD HH:MM, optional)",
                  foreground="gray").pack(side=tk.LEFT)

        row3 = ttk.Frame(dlg); row3.pack(fill="x", **pad)
        ttk.Label(row3, text="Priority:", width=10).pack(side=tk.LEFT)
        prio_var = tk.StringVar(
            value=(task.get("priority", "normal") if task else "normal"))
        ttk.Combobox(row3, textvariable=prio_var,
                     values=["low", "normal", "high"],
                     state="readonly", width=10).pack(side=tk.LEFT)

        row3b = ttk.Frame(dlg); row3b.pack(fill="x", **pad)
        ttk.Label(row3b, text="Remind:", width=10).pack(side=tk.LEFT)
        if task is not None and task.get("reminder_minutes") is not None:
            rm_val = str(task.get("reminder_minutes"))
        else:
            rm_val = str(self.app.app_config.get("default_reminder_minutes", 30))
        reminder_var = tk.StringVar(value=rm_val)
        ttk.Entry(row3b, textvariable=reminder_var, width=10).pack(side=tk.LEFT)
        ttk.Label(row3b, text="  minutes before due (0 = off)",
                  foreground="gray").pack(side=tk.LEFT)

        row4 = ttk.Frame(dlg); row4.pack(fill="x", **pad)
        ttk.Label(row4, text="Description:").pack(anchor="w")
        desc_text = tk.Text(dlg, height=3, width=60, wrap=tk.WORD)
        desc_text.pack(fill="x", padx=10, pady=(0, 6))
        if task:
            desc_text.insert("1.0", task.get("description", ""))

        def _save():
            try:
                t = title_var.get().strip()
                if not t:
                    raise ValueError("Title is required")

                due_raw = due_var.get().strip()
                due_norm = None
                if due_raw:
                    due_norm = _parse_ts_strict(due_raw)
                    if not due_norm:
                        raise ValueError("Due date must be a valid YYYY-MM-DD HH:MM")

                rm_raw = reminder_var.get().strip()
                try:
                    rm_int = int(rm_raw)
                except ValueError:
                    raise ValueError("Remind must be a whole number of minutes")
                if rm_int < 0 or rm_int > 10080:
                    raise ValueError("Remind must be between 0 and 10080 minutes")

                d = desc_text.get("1.0", "end-1c").strip()
                prio = prio_var.get() or "normal"
                if task:
                    _update_task(task["id"], t, d, due_norm, prio, rm_int)
                else:
                    _add_task(t, d, due_norm, prio, rm_int)
                dlg.destroy()
                self.refresh()
                self.app.flash_status("Status: Task saved", "green")
            except Exception as ex:
                messagebox.showerror("Error", str(ex), parent=dlg)

        btn_row = ttk.Frame(dlg); btn_row.pack(fill="x", padx=10, pady=(4, 10))
        ttk.Button(btn_row, text="Cancel", command=dlg.destroy).pack(side=tk.RIGHT)
        ttk.Button(btn_row, text="Save", command=_save
                   ).pack(side=tk.RIGHT, padx=(0, 6))