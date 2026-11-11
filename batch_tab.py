"""Batch tab: clone-folder task runner with auto-progress approach cycling."""
import os
import queue
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
from datetime import datetime

from core.batch_editor import (
    BatchConfig, BatchTask, BatchRunner, EventType,
)

DEFAULT_CHECKS = ["syntax", "not_identical", "diff_bounded"]


class BatchTab(ttk.Frame):
    def __init__(self, parent, app):
        super().__init__(parent, padding=8)
        self.app = app
        self.runner: BatchRunner | None = None
        self.tasks: list[BatchTask] = []
        self._pending: dict = {}

        self.source_var = tk.StringVar()
        self.clone_var = tk.StringVar()
        self.auto_var = tk.BooleanVar(value=False)
        self.max_retries_var = tk.IntVar(value=1)
        self.diff_ratio_var = tk.DoubleVar(value=60)
        self.check_vars = {c: tk.BooleanVar(value=True) for c in DEFAULT_CHECKS}
        self.check_vars["ruff"] = tk.BooleanVar(value=False)
        self.check_vars["llm_review"] = tk.BooleanVar(value=False)

        self._build_ui()
        self._refresh_task_tree()

    def _build_ui(self):
        self.columnconfigure(0, weight=1)
        self.rowconfigure(0, weight=1)

        paned = ttk.PanedWindow(self, orient="horizontal")
        paned.grid(row=0, column=0, sticky="nsew")

        left = ttk.Frame(paned, padding=4)
        paned.add(left, weight=1)

        cfg = ttk.LabelFrame(left, text="Setup", padding=6)
        cfg.pack(fill="x", pady=(0, 6))

        ttk.Label(cfg, text="Source folder:").grid(row=0, column=0, sticky="w")
        ttk.Entry(cfg, textvariable=self.source_var).grid(
            row=0, column=1, sticky="ew", padx=4)
        ttk.Button(cfg, text="Browse…",
                   command=self._pick_source).grid(row=0, column=2)

        ttk.Label(cfg, text="Clone to:").grid(row=1, column=0, sticky="w", pady=(4, 0))
        ttk.Entry(cfg, textvariable=self.clone_var).grid(
            row=1, column=1, sticky="ew", padx=4, pady=(4, 0))
        ttk.Button(cfg, text="Browse…",
                   command=self._pick_clone).grid(row=1, column=2, pady=(4, 0))

        opts = ttk.Frame(cfg)
        opts.grid(row=2, column=0, columnspan=3, sticky="ew", pady=(6, 0))
        ttk.Checkbutton(
            opts,
            text="Auto-progress — cycle through approaches until validation "
                 "passes, then write; fall back to manual review only if all "
                 "approaches fail",
            variable=self.auto_var).pack(anchor="w")

        row = ttk.Frame(opts)
        row.pack(fill="x", pady=(4, 0))
        ttk.Label(row, text="Manual max retries:").pack(side=tk.LEFT)
        ttk.Spinbox(row, from_=0, to=5, width=3,
                    textvariable=self.max_retries_var).pack(side=tk.LEFT, padx=(4, 12))
        ttk.Label(row, text="Max diff %:").pack(side=tk.LEFT)
        ttk.Spinbox(row, from_=10, to=100, width=4,
                    textvariable=self.diff_ratio_var).pack(side=tk.LEFT, padx=(4, 12))

        checks = ttk.Frame(opts)
        checks.pack(fill="x", pady=(4, 0))
        ttk.Label(checks, text="Checks:").pack(side=tk.LEFT)
        for name, var in self.check_vars.items():
            ttk.Checkbutton(checks, text=name, variable=var).pack(
                side=tk.LEFT, padx=(6, 0))

        cfg.columnconfigure(1, weight=1)

        actions = ttk.Frame(left)
        actions.pack(fill="x", pady=(0, 6))
        self.btn_start = ttk.Button(actions, text="▶ Start",
                                    command=self._start)
        self.btn_start.pack(side=tk.LEFT)
        self.btn_pause = ttk.Button(actions, text="⏸ Pause", state="disabled",
                                    command=self._toggle_pause)
        self.btn_pause.pack(side=tk.LEFT, padx=(4, 0))
        self.btn_stop = ttk.Button(actions, text="⏹ Stop", state="disabled",
                                   command=self._stop)
        self.btn_stop.pack(side=tk.LEFT, padx=(4, 0))
        ttk.Button(actions, text="📂 Clear clone",
                   command=self._clear_clone).pack(side=tk.RIGHT)

        tasks_frame = ttk.LabelFrame(left, text="Tasks", padding=4)
        tasks_frame.pack(fill="both", expand=True)

        cols = ("status", "file", "description")
        self.tree = ttk.Treeview(tasks_frame, columns=cols, show="headings",
                                 height=12)
        self.tree.heading("status", text="")
        self.tree.heading("file", text="File")
        self.tree.heading("description", text="Task")
        self.tree.column("status", width=40, anchor="center", stretch=False)
        self.tree.column("file", width=140, stretch=False)
        self.tree.column("description", width=340)
        self.tree.pack(fill="both", expand=True, side=tk.LEFT)
        sb = ttk.Scrollbar(tasks_frame, orient="vertical",
                           command=self.tree.yview)
        sb.pack(side=tk.RIGHT, fill="y")
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.bind("<Double-Button-1>", lambda e: self._edit_task())

        task_btns = ttk.Frame(left)
        task_btns.pack(fill="x", pady=(4, 0))
        ttk.Button(task_btns, text="+ Add", command=self._add_task).pack(side=tk.LEFT)
        ttk.Button(task_btns, text="Edit", command=self._edit_task).pack(side=tk.LEFT, padx=(4, 0))
        ttk.Button(task_btns, text="Remove", command=self._remove_task).pack(side=tk.LEFT, padx=(4, 0))
        ttk.Button(task_btns, text="↑", width=3,
                   command=lambda: self._move(-1)).pack(side=tk.LEFT, padx=(12, 0))
        ttk.Button(task_btns, text="↓", width=3,
                   command=lambda: self._move(1)).pack(side=tk.LEFT, padx=(4, 0))
        ttk.Button(task_btns, text="Load…",
                   command=self._load_tasks).pack(side=tk.RIGHT, padx=(4, 0))
        ttk.Button(task_btns, text="Save…",
                   command=self._save_tasks).pack(side=tk.RIGHT)

        right = ttk.Frame(paned, padding=4)
        paned.add(right, weight=2)
        right.rowconfigure(0, weight=1)
        right.columnconfigure(0, weight=1)

        inner_paned = ttk.PanedWindow(right, orient="vertical")
        inner_paned.grid(row=0, column=0, sticky="nsew")

        top = ttk.Frame(inner_paned)
        inner_paned.add(top, weight=1)
        top.columnconfigure(0, weight=1)
        top.rowconfigure(0, weight=1)

        self.live_output = tk.Text(top, height=8, wrap=tk.WORD,
                                   bg="#1e1e1e", fg="#d4d4d4",
                                   font=("Consolas", 9), state=tk.DISABLED)
        self.live_output.grid(row=0, column=0, sticky="nsew")
        sb2 = ttk.Scrollbar(top, orient="vertical", command=self.live_output.yview)
        sb2.grid(row=0, column=1, sticky="ns")
        self.live_output.configure(yscrollcommand=sb2.set)

        self.validation_frame = ttk.LabelFrame(top, text="Validation", padding=4)
        self.validation_frame.grid(row=1, column=0, columnspan=2,
                                   sticky="ew", pady=(4, 0))
        self._render_validation_placeholder()

        bottom = ttk.Frame(inner_paned)
        inner_paned.add(bottom, weight=2)
        bottom.columnconfigure(0, weight=1)
        bottom.rowconfigure(1, weight=1)

        self.notebook = ttk.Notebook(bottom)
        self.notebook.grid(row=0, column=0, sticky="nsew")

        self.proposed_text = tk.Text(self.notebook, wrap=tk.NONE,
                                     bg="#1e1e1e", fg="#d4d4d4",
                                     font=("Consolas", 9), undo=True)
        self.notebook.add(self.proposed_text, text="Proposed")

        self.diff_text = tk.Text(self.notebook, wrap=tk.NONE,
                                 bg="#1e1e1e", fg="#d4d4d4",
                                 font=("Consolas", 9), state=tk.DISABLED)
        self.notebook.add(self.diff_text, text="Diff")
        self.diff_text.tag_config("add", foreground="#8ee08e")
        self.diff_text.tag_config("del", foreground="#f48771")
        self.diff_text.tag_config("hdr", foreground="#4ec9b0")

        feedback_row = ttk.Frame(bottom)
        feedback_row.grid(row=1, column=0, sticky="ew", pady=(4, 0))
        feedback_row.columnconfigure(1, weight=1)
        ttk.Label(feedback_row, text="Feedback:").grid(row=0, column=0, sticky="w")
        self.feedback_var = tk.StringVar()
        ttk.Entry(feedback_row, textvariable=self.feedback_var).grid(
            row=0, column=1, sticky="ew", padx=4)

        btn_row = ttk.Frame(bottom)
        btn_row.grid(row=2, column=0, sticky="ew", pady=(6, 0))
        self.btn_approve = ttk.Button(btn_row, text="✓ Approve & Write",
                                      command=self._approve, state="disabled")
        self.btn_approve.pack(side=tk.LEFT)
        self.btn_reject = ttk.Button(btn_row, text="✗ Reject",
                                     command=self._reject, state="disabled")
        self.btn_reject.pack(side=tk.LEFT, padx=(4, 0))
        self.btn_skip = ttk.Button(btn_row, text="→ Skip",
                                   command=self._skip, state="disabled")
        self.btn_skip.pack(side=tk.LEFT, padx=(4, 0))

        log_frame = ttk.LabelFrame(right, text="Log", padding=4)
        log_frame.grid(row=1, column=0, sticky="ew", pady=(4, 0))
        self.log_text = tk.Text(log_frame, height=6, wrap=tk.WORD,
                                bg="#1a1a1a", fg="#aaa",
                                font=("Consolas", 8), state=tk.DISABLED)
        self.log_text.pack(fill="x")

    def _render_validation_placeholder(self):
        for w in self.validation_frame.winfo_children():
            w.destroy()
        ttk.Label(self.validation_frame, text="(no task ready)",
                  foreground="gray").pack(anchor="w")

    def _pick_source(self):
        d = filedialog.askdirectory(title="Source folder", parent=self.winfo_toplevel())
        if d:
            self.source_var.set(d)
            if not self.clone_var.get().strip():
                self.clone_var.set(self._suggest_clone(d))

    def _pick_clone(self):
        d = filedialog.askdirectory(title="Clone destination",
                                    parent=self.winfo_toplevel())
        if d:
            self.clone_var.set(d)

    def _suggest_clone(self, source):
        parent = os.path.dirname(os.path.abspath(source.rstrip(os.sep)))
        base = os.path.basename(os.path.abspath(source.rstrip(os.sep)))
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        return os.path.join(parent, f"{base}_batch_{stamp}")

    def _clear_clone(self):
        d = self.clone_var.get().strip()
        if not d or not os.path.isdir(d):
            messagebox.showinfo("Clear clone", "No clone folder to clear.")
            return
        if not messagebox.askyesno("Clear clone", f"Delete everything in:\n{d}"):
            return
        try:
            for n in os.listdir(d):
                p = os.path.join(d, n)
                if os.path.isdir(p):
                    import shutil as _sh
                    _sh.rmtree(p)
                else:
                    os.remove(p)
            self._log(f"Cleared clone folder: {d}")
        except Exception as e:
            messagebox.showerror("Clear clone", str(e))

    def _add_task(self):
        dlg = _TaskDialog(self, "Add task")
        self.wait_window(dlg)  # <--- FIX: Wait for dialog to close
        if dlg.result:
            file_, desc = dlg.result
            self.tasks.append(BatchTask(description=desc, target_file=file_))
            self._refresh_task_tree()

    def add_tasks(self, new_tasks: list[dict]):
        """Called from the app when the AI generates batch tasks."""
        added = 0
        for t in new_tasks:
            self.tasks.append(BatchTask(description=t["task"], target_file=t["file"]))
            added += 1
        if added:
            self._refresh_task_tree()
            self._log(f"Added {added} task(s) from chat.")

    def _edit_task(self):
        idx = self._selected_task_idx()
        if idx is None:
            return
        t = self.tasks[idx]
        dlg = _TaskDialog(self, "Edit task", initial=(t.target_file, t.description))
        self.wait_window(dlg)  # <--- FIX: Wait for dialog to close
        if dlg.result:
            file_, desc = dlg.result
            t.target_file = file_
            t.description = desc
            self._refresh_task_tree()

    def _remove_task(self):
        idx = self._selected_task_idx()
        if idx is None:
            return
        del self.tasks[idx]
        self._refresh_task_tree()

    def _move(self, delta):
        idx = self._selected_task_idx()
        if idx is None:
            return
        new = max(0, min(len(self.tasks) - 1, idx + delta))
        if new == idx:
            return
        self.tasks[idx], self.tasks[new] = self.tasks[new], self.tasks[idx]
        self._refresh_task_tree()
        self.tree.selection_set(self.tree.get_children()[new])

    def _selected_task_idx(self):
        sel = self.tree.selection()
        if not sel:
            return None
        return self.tree.index(sel[0])

    def _refresh_task_tree(self):
        self.tree.delete(*self.tree.get_children())
        for t in self.tasks:
            badge = {"pending": "○", "running": "⟳", "ready": "?",
                     "applied": "✓", "skipped": "→", "failed": "✗"}.get(t.status, "·")
            self.tree.insert("", "end", values=(badge, t.target_file, t.description))

    def _load_tasks(self):
        path = filedialog.askopenfilename(
            title="Load tasks", parent=self.winfo_toplevel(),
            filetypes=[("Task list", "*.txt"), ("All", "*.*")])
        if not path:
            return
        try:
            loaded = []
            with open(path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.rstrip("\n")
                    if not line.strip() or line.lstrip().startswith("#"):
                        continue
                    if ":::" not in line:
                        continue
                    fpart, dpart = line.split(":::", 1)
                    loaded.append(BatchTask(description=dpart.strip(),
                                            target_file=fpart.strip()))
            self.tasks.extend(loaded)
            self._refresh_task_tree()
            self._log(f"Loaded {len(loaded)} tasks from {path}")
        except Exception as e:
            messagebox.showerror("Load tasks", str(e))

    def _save_tasks(self):
        path = filedialog.asksaveasfilename(
            title="Save tasks", parent=self.winfo_toplevel(),
            defaultextension=".txt",
            filetypes=[("Task list", "*.txt"), ("All", "*.*")])
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8") as f:
                for t in self.tasks:
                    f.write(f"{t.target_file} ::: {t.description}\n")
            self._log(f"Saved {len(self.tasks)} tasks to {path}")
        except Exception as e:
            messagebox.showerror("Save tasks", str(e))

    def _start(self):
        if self.runner and self.runner.is_running():
            return
        source = self.source_var.get().strip()
        clone = self.clone_var.get().strip()
        if not source or not os.path.isdir(source):
            messagebox.showerror("Start", "Pick a valid source folder.")
            return
        if not clone:
            messagebox.showerror("Start", "Pick a clone destination folder.")
            return
        if not self.tasks:
            messagebox.showerror("Start", "Add at least one task.")
            return

        for t in self.tasks:
            t.status = "pending"
            t.attempts = 0
            t.result_notes = ""
        self._refresh_task_tree()
        self._clear_live()
        self._clear_proposed()
        self._clear_diff()
        self._render_validation_placeholder()

        checks = [name for name, var in self.check_vars.items() if var.get()]
        cfg = BatchConfig(
            source_folder=source,
            clone_folder=clone,
            tasks=self.tasks,
            auto_progress=self.auto_var.get(),
            checks=checks,
            max_retries=int(self.max_retries_var.get()),
            max_diff_ratio=float(self.diff_ratio_var.get()) / 100.0,
        )

        self.runner = BatchRunner(
            cfg,
            llm_fn=self._llm_call,
            on_event=self._emit_event,
        )
        self.runner.start()
        self.btn_start.config(state="disabled")
        self.btn_pause.config(state="normal", text="⏸ Pause")
        self.btn_stop.config(state="normal")
        self._log("Batch started.")

    def _toggle_pause(self):
        if not self.runner:
            return
        if self.btn_pause.cget("text").startswith("⏸"):
            self.runner.pause()
            self.btn_pause.config(text="▶ Resume")
        else:
            self.runner.resume()
            self.btn_pause.config(text="⏸ Pause")

    def _stop(self):
        if self.runner:
            self.runner.stop()
            self._log("Stop requested.")

    def _llm_call(self, prompt: str, on_chunk, timeout=None):
        return self.app.stream_completion(prompt, on_chunk, timeout=timeout)

    def _emit_event(self, event_type, payload):
        self.app.ui_queue.put(("batch_event", event_type, payload))

    def handle_event(self, event_type: EventType, payload: dict):
        try:
            self._handle_event(event_type, payload)
        except Exception as e:
            self._log(f"UI event error: {e}")

    def _handle_event(self, event_type, payload):
        if event_type == EventType.LOG:
            self._log(f"[{payload.get('ts','')}] {payload.get('msg','')}")
            return

        if event_type == EventType.BATCH_STARTED:
            mode = "auto" if payload.get("auto_progress") else "manual"
            self._log(f"Cloned to {payload['clone']} ({mode} mode); "
                      f"{payload['task_count']} task(s).")
            if payload.get("approaches"):
                self._log(f"Auto approaches: {', '.join(payload['approaches'])}")
            return

        if event_type == EventType.BATCH_COMPLETED:
            self._log(f"Done. applied={payload['applied']} "
                      f"skipped={payload['skipped']} failed={payload['failed']} "
                      f"/ {payload['total']}")
            self._reset_run_buttons()
            return

        if event_type == EventType.BATCH_ABORTED:
            self._log(f"Aborted: {payload.get('reason','')}")
            self._reset_run_buttons()
            return

        if event_type == EventType.TASK_STARTED:
            self._set_task_status(payload["idx"], "running")
            self._clear_live()
            self._set_live(f"--- Task {payload['idx'] + 1}: {payload['task']} ---\n")
            self._render_validation_placeholder()
            self._disable_action_buttons()
            return

        if event_type == EventType.TASK_STREAM:
            self._append_live(payload.get("chunk", ""))
            return

        if event_type == EventType.TASK_RETRYING:
            nxt = payload.get("next_approach") or "(manual review)"
            checks = ", ".join(payload.get("failed_checks") or []) or "?"
            self._log(f"↻ Task {payload['idx']+1} attempt "
                      f"{payload['attempt']}/{payload['max_attempts']}: "
                      f"{checks} → {nxt}")
            self._render_retrying(payload)
            self._clear_live()
            self._set_live(f"↻ Retrying with approach: {nxt}\n")
            return

        if event_type == EventType.TASK_READY:
            self._pending = payload
            self._set_task_status(payload["idx"], "ready")
            self._render_validation(
                payload.get("validation") or [],
                blocked=payload.get("auto_progress_blocked", False),
                auto_exhausted=payload.get("auto_exhausted", False),
            )
            self._set_proposed(payload.get("new_content", ""))
            self._set_diff(payload.get("diff", ""))
            self.btn_approve.config(state="normal")
            self.btn_reject.config(state="normal")
            self.btn_skip.config(state="normal")
            self.feedback_var.set("")

            if payload.get("auto_exhausted"):
                self._log(
                    f"Task {payload['idx']+1}: all auto approaches exhausted. "
                    "Approve, reject with feedback for one more attempt, or skip.")
            else:
                self._log(
                    f"Task {payload['idx']+1} ready "
                    f"(attempt {payload['attempts']}/{payload['max_attempts']}).")
            return

        if event_type == EventType.TASK_APPLIED:
            self._pending = {}
            self._set_task_status(payload["idx"], "applied")
            self._disable_action_buttons()
            self._log(f"✓ Task {payload['idx']+1} written: "
                      f"{payload['target_file']} ({payload['lines']} lines).")
            
            # --- NEW: Track recent files for Dashboard ---
            try:
                from core.db import _save_app_config
                target = payload.get("target_file", "")
                if target:
                    full_path = os.path.join(self.clone_var.get().strip(), target)
                    recent = self.app.app_config.get("recent_batch_files", [])
                    if full_path in recent:
                        recent.remove(full_path)
                    recent.insert(0, full_path)
                    self.app.app_config["recent_batch_files"] = recent[:5]
                    _save_app_config(self.app.app_config)
            except Exception as e:
                print(f"Recent files save error: {e}")
            # ---------------------------------------------
            return

        if event_type == EventType.TASK_REJECTED:
            self._pending = {}
            self._disable_action_buttons()
            self._log(f"✗ Task {payload['idx']+1} rejected — "
                      f"feedback: {payload.get('feedback','')[:60]}")
            return

        if event_type == EventType.TASK_SKIPPED:
            self._pending = {}
            self._set_task_status(payload["idx"], "skipped")
            self._disable_action_buttons()
            self._log(f"→ Task {payload['idx']+1} skipped.")
            return

        if event_type == EventType.TASK_FAILED:
            self._pending = {}
            self._set_task_status(payload["idx"], "failed")
            self._disable_action_buttons()
            self._log(f"✗ Task {payload['idx']+1} failed: "
                      f"{payload.get('reason','')}")
            return

    def _approve(self):
        if not self.runner or not self._pending:
            return
        edited = self.proposed_text.get("1.0", "end-1c") + "\n"
        self._disable_action_buttons()
        self.runner.approve(edited)

    def _reject(self):
        if not self.runner or not self._pending:
            return
        fb = self.feedback_var.get().strip()
        if not fb:
            fb = "User rejected the change."
        self._disable_action_buttons()
        self.runner.reject(fb)

    def _skip(self):
        if not self.runner or not self._pending:
            return
        self._disable_action_buttons()
        self.runner.skip()

    def _disable_action_buttons(self):
        self.btn_approve.config(state="disabled")
        self.btn_reject.config(state="disabled")
        self.btn_skip.config(state="disabled")

    def _reset_run_buttons(self):
        self.btn_start.config(state="normal")
        self.btn_pause.config(state="disabled", text="⏸ Pause")
        self.btn_stop.config(state="disabled")
        self._disable_action_buttons()

    def _set_task_status(self, idx, status):
        if idx < 0 or idx >= len(self.tasks):
            return
        self.tasks[idx].status = status
        self._refresh_task_tree()

    def _clear_live(self):
        self.live_output.config(state=tk.NORMAL)
        self.live_output.delete("1.0", tk.END)
        self.live_output.config(state=tk.DISABLED)

    def _set_live(self, text):
        self.live_output.config(state=tk.NORMAL)
        self.live_output.insert(tk.END, text)
        self.live_output.see(tk.END)
        self.live_output.config(state=tk.DISABLED)

    def _append_live(self, text):
        if not text:
            return
        self.live_output.config(state=tk.NORMAL)
        self.live_output.insert(tk.END, text)
        self.live_output.see(tk.END)
        self.live_output.config(state=tk.DISABLED)

    def _render_retrying(self, payload):
        for w in self.validation_frame.winfo_children():
            w.destroy()
        nxt = payload.get("next_approach")
        header = ("↻ Auto retry — trying a different approach"
                  if nxt else
                  "↻ All approaches exhausted — pausing for review")
        ttk.Label(self.validation_frame, text=header,
                  foreground="#e8b84b").pack(anchor="w")
        for check in payload.get("failed_checks") or []:
            ttk.Label(self.validation_frame,
                      text=f"✗ {check}: failed",
                      foreground="#f48771").pack(anchor="w")
        if nxt:
            ttk.Label(self.validation_frame,
                      text=f"next approach → {nxt}",
                      foreground="#4ec9b0").pack(anchor="w")

    def _render_validation(self, results, blocked, auto_exhausted=False):
        for w in self.validation_frame.winfo_children():
            w.destroy()
        if auto_exhausted:
            ttk.Label(
                self.validation_frame,
                text=("⚠ All auto approaches exhausted. Approve to write "
                      "anyway, reject with feedback for one final "
                      "user-guided attempt, or skip."),
                foreground="#f48771").pack(anchor="w")
        elif blocked:
            ttk.Label(
                self.validation_frame,
                text="⚠ Validation failed. Approve or reject manually.",
                foreground="#f48771").pack(anchor="w")
        for r in results:
            mark = "✓" if r["passed"] else "✗"
            color = "#4ec9b0" if r["passed"] else "#f48771"
            req = "" if r.get("required", True) else " (optional)"
            ttk.Label(self.validation_frame,
                      text=f"{mark} {r['check']}{req}: {r['detail']}",
                      foreground=color).pack(anchor="w")

    def _clear_proposed(self):
        self.proposed_text.delete("1.0", tk.END)

    def _set_proposed(self, content):
        self.proposed_text.delete("1.0", tk.END)
        self.proposed_text.insert("1.0", content)

    def _clear_diff(self):
        self.diff_text.config(state=tk.NORMAL)
        self.diff_text.delete("1.0", tk.END)
        self.diff_text.config(state=tk.DISABLED)

    def _set_diff(self, diff):
        self.diff_text.config(state=tk.NORMAL)
        self.diff_text.delete("1.0", tk.END)
        for line in diff.splitlines():
            tag = None
            if line.startswith(("+++", "---")):
                tag = "hdr"
            elif line.startswith("+"):
                tag = "add"
            elif line.startswith("-"):
                tag = "del"
            if tag:
                self.diff_text.insert(tk.END, line + "\n", tag)
            else:
                self.diff_text.insert(tk.END, line + "\n")
        self.diff_text.config(state=tk.DISABLED)

    def _log(self, msg):
        self.log_text.config(state=tk.NORMAL)
        self.log_text.insert(tk.END, msg + "\n")
        self.log_text.see(tk.END)
        self.log_text.config(state=tk.DISABLED)

    def stop_if_running(self):
        try:
            if self.runner and self.runner.is_running():
                self.runner.stop()
        except Exception:
            pass


class _TaskDialog(tk.Toplevel):
    def __init__(self, parent_tab, title, initial=None):
        super().__init__(parent_tab.winfo_toplevel())
        self.title(title)
        self.transient(parent_tab.winfo_toplevel())
        self.grab_set()
        self.resizable(False, False)
        self.result = None
        parent_tab_ref = parent_tab

        pad = {"padx": 10, "pady": 4}
        row1 = ttk.Frame(self); row1.pack(fill="x", **pad)
        ttk.Label(row1, text="Target file:", width=12).pack(side=tk.LEFT)
        self.file_var = tk.StringVar(value=(initial[0] if initial else ""))
        ttk.Entry(row1, textvariable=self.file_var, width=48).pack(
            side=tk.LEFT, fill="x", expand=True)
        ttk.Button(row1, text="Browse…",
                   command=lambda: self._browse(parent_tab_ref)).pack(
                       side=tk.LEFT, padx=(4, 0))
        ttk.Label(self, text="Relative to source folder, e.g. core/db.py",
                  foreground="gray").pack(anchor="w", padx=10)

        row2 = ttk.Frame(self); row2.pack(fill="x", **pad)
        ttk.Label(row2, text="Task:").pack(anchor="w")
        self.desc_text = tk.Text(self, height=4, width=56, wrap=tk.WORD)
        self.desc_text.pack(fill="x", padx=10, pady=(0, 6))
        if initial:
            self.desc_text.insert("1.0", initial[1])

        btn_row = ttk.Frame(self); btn_row.pack(fill="x", padx=10, pady=(0, 10))
        ttk.Button(btn_row, text="Cancel", command=self.destroy).pack(side=tk.RIGHT)
        ttk.Button(btn_row, text="OK", command=self._ok).pack(side=tk.RIGHT, padx=(0, 6))

    def _browse(self, parent_tab):
        src = parent_tab.source_var.get().strip()
        d = filedialog.askopenfilename(
            title="Choose file in source folder",
            initialdir=src or None, parent=self)
        if d and src:
            try:
                rel = os.path.relpath(d, src)
                self.file_var.set(rel)
            except Exception:
                self.file_var.set(d)

    def _ok(self):
        f = self.file_var.get().strip()
        d = self.desc_text.get("1.0", "end-1c").strip()
        if not f or not d:
            messagebox.showerror("Missing",
                                 "Both file and task are required.",
                                 parent=self)
            return
        self.result = (f, d)
        self.destroy()