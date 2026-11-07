"""Sidebar: conversations, models, personas, notifier config."""
import os
import tkinter as tk
from tkinter import ttk
from datetime import datetime

from core.db import (
    NO_PERSONA, PERSONALITIES_FILE,
    _get_conversations, _save_app_config,
)


class ToolTip:
    def __init__(self, widget):
        self.widget = widget
        self.tipwindow = None
        self.current_text = ""

    def showtip(self, text, x, y):
        if not text:
            self.hidetip()
            return
        if self.tipwindow and self.current_text == text:
            self.tipwindow.wm_geometry("+%d+%d" % (x, y))
            return
        self.hidetip()
        self.current_text = text
        self.tipwindow = tw = tk.Toplevel(self.widget)
        tw.wm_overrideredirect(1)
        tw.wm_geometry("+%d+%d" % (x, y))
        label = tk.Label(tw, text=text, justify=tk.LEFT,
                         background="#ffffe0", fg="#000000", relief=tk.SOLID, borderwidth=1,
                         font=("Segoe UI", 9))
        label.pack(ipadx=1)

    def hidetip(self):
        self.current_text = ""
        tw = self.tipwindow
        self.tipwindow = None
        if tw:
            tw.destroy()


class Sidebar(ttk.Frame):
    def __init__(self, parent, app):
        super().__init__(parent, padding=12)
        self.app = app
        self._suppress_listbox_load = False
        self.conv_id_map = {}
        self.model_metadata = {}
        self.tooltip = None
        self._build()

    # --- Build ------------------------------------------------------------
    def _build(self):
        self.rowconfigure(2, weight=2)
        self.rowconfigure(5, weight=1)
        self.rowconfigure(8, weight=1)
        self.columnconfigure(0, weight=1)

        # --- Conversations ---
        ttk.Label(self, text="💬 Conversations",
                  font=("Segoe UI", 10, "bold")).grid(row=0, column=0, sticky="w", pady=(0, 3))

        self.conv_filter_var = tk.StringVar()
        self.conv_filter_var.trace_add(
            "write", lambda *_: self.refresh_conversations())
        filter_frame = ttk.Frame(self)
        filter_frame.grid(row=1, column=0, sticky="ew", pady=(0, 4))
        filter_frame.columnconfigure(0, weight=1)
        self.conv_filter_entry = ttk.Entry(filter_frame, textvariable=self.conv_filter_var)
        self.conv_filter_entry.grid(row=0, column=0, sticky="ew")
        ttk.Button(filter_frame, text="✕", width=2,
                   command=lambda: self.conv_filter_var.set("")).grid(row=0, column=1, padx=(3, 0))

        self.conversation_listbox = tk.Listbox(self, height=8, selectmode=tk.SINGLE,
                                               exportselection=False, font=("Segoe UI", 9))
        self.conversation_listbox.grid(row=2, column=0, sticky="nsew", pady=(0, 4))
        self.conversation_listbox.bind("<<ListboxSelect>>", self._on_conversation_selected)
        self.conversation_listbox.bind("<Button-3>", self._show_conversation_context_menu)
        self.conversation_listbox.bind("<Button-2>", self._show_conversation_context_menu)
        self.conversation_listbox.bind("<Control-Button-1>", self._show_conversation_context_menu)

        conv_btn_frame = ttk.Frame(self)
        conv_btn_frame.grid(row=3, column=0, sticky="ew", pady=(0, 10))
        conv_btn_frame.columnconfigure(0, weight=1)
        conv_btn_frame.columnconfigure(1, weight=1)
        ttk.Button(conv_btn_frame, text="+ New Chat",
                   command=self.app._new_conversation).grid(row=0, column=0, sticky="ew", padx=(0, 3))
        ttk.Button(conv_btn_frame, text="🗑 Delete",
                   command=self.app._delete_current_conversation).grid(row=0, column=1, sticky="ew", padx=(3, 0))

        # --- Models ---
        ttk.Label(self, text="🤖 Select Model",
                  font=("Segoe UI", 10, "bold")).grid(row=4, column=0, sticky="w", pady=(0, 3))
        self.model_listbox = tk.Listbox(self, height=5, selectmode=tk.SINGLE,
                                        exportselection=False, font=("Consolas", 9))
        self.model_listbox.grid(row=5, column=0, sticky="nsew", pady=(0, 6))
        self.model_listbox.bind("<<ListboxSelect>>", self._on_model_select)
        self.model_listbox.bind("<Motion>", self._on_model_motion)
        self.model_listbox.bind("<Leave>", lambda e: self.tooltip.hidetip())
        self.tooltip = ToolTip(self.model_listbox)
        ttk.Button(self, text="🔄 Sync & Reload Models",
                   command=self.app.load_and_sync_models).grid(row=6, column=0, sticky="ew", pady=(0, 8))

        # --- Personas ---
        ttk.Label(self, text="💼 Select Professional Role",
                  font=("Segoe UI", 10, "bold")).grid(row=7, column=0, sticky="w", pady=(0, 3))
        self.persona_listbox = tk.Listbox(self, height=4, selectmode=tk.SINGLE,
                                          exportselection=False, font=("Segoe UI", 9))
        self.persona_listbox.grid(row=8, column=0, sticky="nsew", pady=(0, 6))
        self.persona_listbox.bind("<<ListboxSelect>>", self._on_persona_select)
        ttk.Button(self, text="🔄 Reload Personalities",
                   command=self.app.reload_personalities).grid(row=9, column=0, sticky="ew", pady=(0, 8))

        # --- Notifier ---
        ttk.Label(self, text="🔔 Phone Reminders",
                  font=("Segoe UI", 10, "bold")).grid(row=10, column=0, sticky="w", pady=(0, 3))

        rem_frame = ttk.Frame(self)
        rem_frame.grid(row=11, column=0, sticky="ew", pady=(0, 8))
        rem_frame.columnconfigure(1, weight=1)

        ttk.Label(rem_frame, text="ntfy topic:").grid(row=0, column=0, sticky="w")
        self.ntfy_topic_var = tk.StringVar(value=self.app.notifier_config.get("ntfy_topic", ""))
        ttk.Entry(rem_frame, textvariable=self.ntfy_topic_var).grid(row=0, column=1, sticky="ew", padx=(6, 0))
        self.ntfy_topic_var.trace_add("write", lambda *_: self.app._on_notifier_config_changed())

        ttk.Label(rem_frame, text="Warn (min):").grid(row=1, column=0, sticky="w", pady=(4, 0))
        lead_str = ", ".join(str(x) for x in self.app.notifier_config.get("lead_minutes", [15, 60]))
        self.lead_minutes_var = tk.StringVar(value=lead_str)
        ttk.Entry(rem_frame, textvariable=self.lead_minutes_var).grid(
            row=1, column=1, sticky="ew", padx=(6, 0), pady=(4, 0))
        self.lead_minutes_var.trace_add("write", lambda *_: self.app._on_notifier_config_changed())

        self.reminder_enabled_var = tk.BooleanVar(value=bool(self.app.notifier_config.get("enabled")))
        ttk.Checkbutton(rem_frame, text="Enable reminders",
                        variable=self.reminder_enabled_var,
                        command=self.app._on_notifier_config_changed).grid(
                            row=2, column=0, columnspan=2, sticky="w", pady=(4, 0))

        btn_rem = ttk.Frame(rem_frame)
        btn_rem.grid(row=3, column=0, columnspan=2, sticky="ew", pady=(4, 0))
        ttk.Button(btn_rem, text="Test", command=self.app._test_notification).pack(side=tk.LEFT)
        ttk.Button(btn_rem, text="Help", command=self.app._show_ntfy_help).pack(side=tk.LEFT, padx=(4, 0))

        # --- Stats ---
        self.history_label = ttk.Label(self, text="Memory: 0 messages",
                                       font=("Segoe UI", 9, "italic"), foreground="gray")
        self.history_label.grid(row=12, column=0, sticky="w", pady=(4, 0))

        self.ctx_label = ttk.Label(self, text="Context: ~0 / 0 tok",
                                   font=("Segoe UI", 8), foreground="#666")
        self.ctx_label.grid(row=13, column=0, sticky="w", pady=(2, 4))

        # --- Actions ---
        action_frame = ttk.Frame(self)
        action_frame.grid(row=14, column=0, sticky="ew", pady=(2, 0))
        action_frame.columnconfigure(0, weight=1)
        action_frame.columnconfigure(1, weight=1)
        ttk.Button(action_frame, text="⚙️ Settings",
                   command=self.app._open_settings_dialog).grid(row=0, column=0, sticky="ew", padx=(0, 3))
        ttk.Button(action_frame, text="🔍 Show Prompt",
                   command=self.app._preview_system_prompt).grid(row=0, column=1, sticky="ew", padx=(3, 0))

        # Populate initial state
        self.refresh_personas()
        self.refresh_conversations()

    # --- Conversations ----------------------------------------------------
    def _format_conv_label(self, title, updated_at_str):
        try:
            dt = datetime.strptime(updated_at_str, "%Y-%m-%d %H:%M:%S")
            secs = (datetime.now() - dt).total_seconds()
            if secs < 60: ago = "now"
            elif secs < 3600: ago = f"{int(secs/60)}m"
            elif secs < 86400: ago = f"{int(secs/3600)}h"
            elif (datetime.now() - dt).days < 7: ago = f"{(datetime.now() - dt).days}d"
            else: ago = dt.strftime("%b %d")
            t = title[:30] + ("…" if len(title) > 30 else "")
            return f"{t}  ·  {ago}"
        except Exception:
            return title[:40]

    def refresh_conversations(self):
        prev = self.app.current_conversation_id
        filter_str = self.conv_filter_var.get().strip().lower()
        self.conversation_listbox.delete(0, tk.END)
        self.conv_id_map = {}
        for c in _get_conversations():
            if filter_str and filter_str not in c["title"].lower():
                continue
            idx = self.conversation_listbox.size()
            self.conversation_listbox.insert(tk.END,
                                             self._format_conv_label(c["title"], c["updated_at"]))
            self.conv_id_map[idx] = c["id"]
        if prev is not None:
            for i in range(self.conversation_listbox.size()):
                if self.conv_id_map.get(i) == prev:
                    self.conversation_listbox.selection_clear(0, tk.END)
                    self.conversation_listbox.selection_set(i)
                    self.conversation_listbox.see(i)
                    break

    def select_first(self):
        if self.conversation_listbox.size() > 0:
            self.conversation_listbox.selection_clear(0, tk.END)
            self.conversation_listbox.selection_set(0)
            return self.conv_id_map.get(0)
        return None

    def _on_conversation_selected(self, event=None):
        if self._suppress_listbox_load:
            return
        if self.app.is_streaming:
            self.app.flash_status("Status: Wait for current response to finish", "orange")
            for i in range(self.conversation_listbox.size()):
                if self.conv_id_map.get(i) == self.app.current_conversation_id:
                    self.conversation_listbox.selection_clear(0, tk.END)
                    self.conversation_listbox.selection_set(i)
                    break
            return
        sel = self.conversation_listbox.curselection()
        if not sel:
            return
        cid = self.conv_id_map.get(sel[0])
        if cid is None or cid == self.app.current_conversation_id:
            return
        self.app._load_conversation(cid)

    def _show_conversation_context_menu(self, event):
        idx = self.conversation_listbox.nearest(event.y)
        if idx is None or idx < 0 or idx >= self.conversation_listbox.size():
            return
        self._suppress_listbox_load = True
        try:
            self.conversation_listbox.selection_clear(0, tk.END)
            self.conversation_listbox.selection_set(idx)
            self.conversation_listbox.activate(idx)
        finally:
            self._suppress_listbox_load = False
        cid = self.conv_id_map.get(idx)
        if cid is None:
            return
        menu = tk.Menu(self.winfo_toplevel(), tearoff=0)
        menu.add_command(label="Open", command=lambda: self.app._load_conversation(cid))
        menu.add_command(label="Rename…", command=lambda: self.app._rename_conversation_dialog(cid))
        menu.add_command(label="Export to Markdown…", command=lambda: self.app._export_conversation(cid))
        menu.add_separator()
        menu.add_command(label="Delete", command=lambda: self.app._delete_conversation_by_id(cid))
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    def focus_filter(self):
        try:
            self.conv_filter_entry.focus_set()
        except Exception:
            self.conversation_listbox.focus_set()

    # --- Models -----------------------------------------------------------
    def _on_model_motion(self, event):
        idx = self.model_listbox.nearest(event.y)
        if idx >= 0 and idx < self.model_listbox.size():
            model = self.model_listbox.get(idx)
            text = self.model_metadata.get(model)
            if text:
                x = self.model_listbox.winfo_rootx() + event.x + 15
                y = self.model_listbox.winfo_rooty() + event.y + 15
                self.tooltip.showtip(text, x, y)
            else:
                self.tooltip.hidetip()
        else:
            self.tooltip.hidetip()

    def _on_model_select(self, event):
        sel = self.model_listbox.curselection()
        if sel:
            v = self.model_listbox.get(sel[0])
            if not v.startswith(("Error", "Loading", "Checking", "(")):
                self.app.current_model.set(v)
                self.app.app_config["model"] = v
                _save_app_config(self.app.app_config)

    def set_models(self, models, metadata=None):
        if metadata:
            self.model_metadata.update(metadata)
        self.model_listbox.delete(0, tk.END)
        for m in models:
            self.model_listbox.insert(tk.END, m)
        if not models or models[0].startswith(("Error", "(")):
            return
        saved = self.app.app_config.get("model", "")
        if saved and saved in models:
            idx = models.index(saved)
            self.model_listbox.selection_set(idx)
            self.app.current_model.set(saved)
        else:
            self.model_listbox.selection_set(0)
            self.app.current_model.set(models[0])
            self.app.app_config["model"] = models[0]
            _save_app_config(self.app.app_config)

    def set_loading_message(self, msg):
        self.model_listbox.delete(0, tk.END)
        self.model_listbox.insert(tk.END, msg)

    # --- Personas ---------------------------------------------------------
    def _on_persona_select(self, event):
        sel = self.persona_listbox.curselection()
        if sel:
            persona = self.persona_listbox.get(sel[0])
            self.app.current_persona.set(persona)
            self.app.app_config["persona"] = persona
            _save_app_config(self.app.app_config)

    def refresh_personas(self):
        self.persona_listbox.delete(0, tk.END)
        self.persona_listbox.insert(tk.END, NO_PERSONA)
        for p in self.app.personalities.keys():
            self.persona_listbox.insert(tk.END, p)
        saved_persona = self.app.app_config.get("persona", NO_PERSONA)
        if saved_persona == NO_PERSONA or saved_persona in self.app.personalities:
            idx = 0 if saved_persona == NO_PERSONA else list(self.app.personalities.keys()).index(saved_persona) + 1
            self.persona_listbox.selection_set(idx)
            self.app.current_persona.set(saved_persona)
        else:
            self.persona_listbox.selection_set(0)
            self.app.current_persona.set(NO_PERSONA)

    # --- Stats ------------------------------------------------------------
    def update_history_label(self, count):
        self.history_label.config(text=f"Memory: {count} messages")

    def update_ctx_label(self, approx, limit, color):
        try:
            self.ctx_label.config(text=f"Context: ~{approx} / {limit} tok", foreground=color)
        except Exception:
            pass