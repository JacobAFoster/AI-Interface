"""Entry point: LocalAIApp holds state and wires the UI panels together."""
import os
import json
import time
import threading
import subprocess
import urllib.request
import urllib.error
import tkinter as tk
from tkinter import ttk, messagebox, simpledialog, filedialog

from core.db import (
    _migrate_cwd_files, _init_db, _save_app_config, _db,
    _load_app_config,
    _get_conversations, _get_messages, _create_conversation,
    _add_message, _delete_message, _delete_conversation, _rename_conversation,
    _trim_orphans_in_db,
    _add_task, _delete_task,
    NO_PERSONA, PERSONALITIES_FILE, MODELS_FILE,
    DEFAULT_REMINDER_MINUTES,
)
from core.parsing import (
    _parse_ts_relaxed,
    _extract_calendar_events, _extract_task_blocks,
)
from core.context import _calendar_context, _code_output_rules
from core.reminders import (
    ReminderService, _load_notifier_config, _save_notifier_config, _send_ntfy,
)
from ui.sidebar import Sidebar
from ui.chat import ChatPanel
from ui.calendar import CalendarTab
from ui.tasks import TaskTab


class LocalAIApp:
    MAX_HISTORY_MESSAGES = 20
    OLLAMA_CHAT_URL = "http://localhost:11434/api/chat"

    def __init__(self, root):
        _migrate_cwd_files()
        _init_db()

        self.root = root
        self.root.title("Local AI Dev & Business Hub")

        saved_geo = None
        try:
            saved_geo = _load_app_config().get("window_geometry")
        except Exception:
            pass
        self.root.geometry(saved_geo if saved_geo else "1280x840")

        style = ttk.Style()
        style.theme_use("clam")

        # ---- State ----
        self.personalities = {}
        self.load_personalities_file()

        self.notifier_config = _load_notifier_config()
        self.app_config = _load_app_config()

        self._notifier_save_id = None
        self.current_model = tk.StringVar(value="")
        self.current_persona = tk.StringVar(value=NO_PERSONA)
        self.is_streaming = False
        self.conversation_history = []
        self.current_conversation_id = None

        self._stream_cancel = threading.Event()
        self._current_response = None

        # ---- Layout ----
        self.root.columnconfigure(1, weight=1)
        self.root.rowconfigure(0, weight=1)

        self.sidebar = Sidebar(self.root, self)
        self.sidebar.grid(row=0, column=0, sticky="nsew")

        self.notebook = ttk.Notebook(self.root)
        self.notebook.grid(row=0, column=1, sticky="nsew")

        self.chat_panel = ChatPanel(self.notebook, self)
        self.notebook.add(self.chat_panel, text="💬 Chat")

        self.calendar_tab = CalendarTab(self.notebook, self)
        self.notebook.add(self.calendar_tab, text="📅 Calendar")

        self.tasks_tab = TaskTab(self.notebook, self)
        self.notebook.add(self.tasks_tab, text="📋 Tasks")

        self.notebook.bind("<<NotebookTabChanged>>", self._on_tab_changed)

        # ---- Background service ----
        self.reminder_service = ReminderService(self)
        self.reminder_service.start()

        # ---- Global bindings ----
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.bind("<Control-n>", lambda e: self._new_conversation())
        self.root.bind("<Control-l>", lambda e: self.chat_panel._clear_terminal_display())
        self.root.bind("<Control-f>", lambda e: self._focus_filter())
        self.root.bind("<Escape>", self._handle_escape)

        # ---- Initial conversation ----
        self.sidebar.refresh_conversations()
        first = self.sidebar.select_first()
        if first is not None:
            self._load_conversation(first)

        self.load_and_sync_models()

        if self.notifier_config.get("enabled") and self.notifier_config.get("ntfy_topic"):
            self.root.after(1500, lambda: self.flash_status(
                f"🔔 Reminders active on topic '{self.notifier_config['ntfy_topic']}'",
                "green", 4000))

    # --- Status delegation ------------------------------------------------
    def flash_status(self, message, color="green", duration=2500):
        self.chat_panel.flash_status(message, color, duration)

    def start_timer(self):
        self.chat_panel.start_timer()

    def stop_timer(self):
        self.chat_panel.stop_timer()

    # --- Personalities ----------------------------------------------------
    def load_personalities_file(self):
        fallback = {
            "Seasoned Tech Lead": "You are an elite veteran software engineer and technical lead.",
            "Startup Founder": "You are a pragmatic, growth-driven startup founder.",
            "Deep Research Analyst": "You are an elite research scientist and investigative analyst."
        }
        try:
            if os.path.exists(PERSONALITIES_FILE):
                with open(PERSONALITIES_FILE, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if line and not line.startswith("#") and "=" in line:
                            k, v = line.split("=", 1)
                            self.personalities[k.strip()] = v.strip()
            
            # If the file exists but is empty, or doesn't exist, use fallback
            if not self.personalities:
                self.personalities = fallback
        except Exception as e:
            print(f"Error loading personalities: {e}")
            self.personalities = fallback

    def reload_personalities(self):
        self.personalities = {}
        self.load_personalities_file()
        self.sidebar.refresh_personas()
        self.app_config["persona"] = NO_PERSONA
        _save_app_config(self.app_config)
        self.flash_status("Status: Personalities reloaded", "green")

    # --- Conversations ----------------------------------------------------
    def _load_conversation(self, cid):
        _trim_orphans_in_db(cid)
        self.current_conversation_id = cid
        messages = _get_messages(cid)
        self.conversation_history = messages[-self._max_history():]
        self.chat_panel.clear_terminal()
        for m in messages:
            self.chat_panel.render_message(m["role"], m["content"])
        self._update_history_label()

        if self.conversation_history and self.conversation_history[-1]["role"] in ("user", "assistant"):
            self.chat_panel.retry_button.config(state="normal")
        else:
            self.chat_panel.retry_button.config(state="disabled")

        self.flash_status(
            f"Status: Loaded conversation ({len(messages)} messages)", "green")

    def _new_conversation(self):
        if self.is_streaming:
            self.flash_status("Status: Wait for current response to finish", "orange")
            return
        self.current_conversation_id = None
        self.conversation_history = []
        self.chat_panel.clear_terminal()
        self.sidebar.conversation_listbox.selection_clear(0, tk.END)
        self._update_history_label()
        self.chat_panel.retry_button.config(state="disabled")
        self.flash_status("Status: New conversation started", "green")

    def _delete_current_conversation(self):
        if self.is_streaming:
            self.flash_status("Status: Wait for current response to finish", "orange")
            return
        if self.current_conversation_id is None:
            self.flash_status("Status: No conversation to delete", "orange")
            return
        self._delete_conversation_by_id(self.current_conversation_id)

    def _delete_conversation_by_id(self, cid):
        if not messagebox.askyesno(
                "Delete Conversation",
                "Delete this conversation? This cannot be undone."):
            return
        _delete_conversation(cid)
        if self.current_conversation_id == cid:
            self.current_conversation_id = None
            self.conversation_history = []
            self.chat_panel.clear_terminal()
        self.sidebar.refresh_conversations()
        if self.current_conversation_id is None:
            first = self.sidebar.select_first()
            if first is not None:
                self._load_conversation(first)
        else:
            self._update_history_label()
        self.flash_status("Status: Conversation deleted", "green")

    def _rename_conversation_dialog(self, cid):
        current = next((c["title"] for c in _get_conversations() if c["id"] == cid), "")
        new_title = simpledialog.askstring(
            "Rename Conversation", "New title:",
            initialvalue=current, parent=self.root)
        if not new_title:
            return
        new_title = new_title.strip()
        if not new_title or new_title == current:
            return
        try:
            _rename_conversation(cid, new_title)
            self.sidebar.refresh_conversations()
            self.flash_status("Status: Conversation renamed", "green")
        except Exception as e:
            self.flash_status(f"Error: {e}", "red")

    def _export_conversation(self, cid):
        try:
            title = next((c["title"] for c in _get_conversations() if c["id"] == cid),
                         "conversation")
            safe_title = "".join(
                [c for c in title if c.isalnum() or c in " -_"]).strip()
            filepath = filedialog.asksaveasfilename(
                parent=self.root,
                defaultextension=".md",
                filetypes=[("Markdown", "*.md"), ("All Files", "*.*")],
                initialfile=f"{safe_title}.md"
            )
            if not filepath:
                return
            msgs = _get_messages(cid)
            md_lines = [f"# {title}\n"]
            for m in msgs:
                role = "User" if m["role"] == "user" else "AI"
                md_lines.append(f"### {role}\n\n{m['content']}\n\n")
            with open(filepath, "w", encoding="utf-8") as f:
                f.write("\n".join(md_lines))
            self.flash_status("Exported to Markdown", "green")
        except Exception as e:
            messagebox.showerror("Export Error", f"Failed to export: {e}",
                                 parent=self.root)

    # --- History / context labels ----------------------------------------
    def _max_history(self):
        try:
            return int(self.app_config.get("max_history_messages",
                                           self.MAX_HISTORY_MESSAGES))
        except (TypeError, ValueError):
            return self.MAX_HISTORY_MESSAGES

    def _update_history_label(self):
        self.sidebar.update_history_label(len(self.conversation_history))
        self._update_ctx_label()

    def _update_ctx_label(self):
        total_chars = sum(len(m.get("content", "")) for m in self.conversation_history)
        approx = total_chars // 4
        try:
            limit = int(self.app_config.get("num_ctx", 4096))
        except (TypeError, ValueError):
            limit = 4096
        if approx > limit * 0.85:
            color = "#c586c0"
        elif approx > limit * 0.6:
            color = "orange"
        else:
            color = "#666"
        self.sidebar.update_ctx_label(approx, limit, color)

    # --- Keyboard / misc --------------------------------------------------
    def _focus_filter(self):
        self.sidebar.focus_filter()

    def _handle_escape(self, event=None):
        if self.is_streaming:
            self._cancel_stream()
        return None

    def _on_tab_changed(self, event=None):
        try:
            idx = self.notebook.index(self.notebook.select())
            if idx == 1:
                self.calendar_tab.refresh()
            elif idx == 2:
                self.tasks_tab.refresh()
        except Exception:
            pass

    def _on_close(self):
        try:
            self.app_config["window_geometry"] = self.root.winfo_geometry()
            _save_app_config(self.app_config)
        except Exception as e:
            print(f"Save geometry error: {e}")
        try:
            self.reminder_service.stop()
        except Exception:
            pass
        try:
            self._stream_cancel.set()
        except Exception:
            pass
        self.root.after(150, self.root.destroy)

    # --- Settings / notifier ----------------------------------------------
    def _open_settings_dialog(self):
        dlg = tk.Toplevel(self.root)
        dlg.title("Settings")
        dlg.transient(self.root)
        dlg.grab_set()
        dlg.resizable(False, False)
        pad = {"padx": 10, "pady": 4}

        row1 = ttk.Frame(dlg); row1.pack(fill="x", **pad)
        ttk.Label(row1, text="num_ctx:", width=18).pack(side=tk.LEFT)
        num_ctx_var = tk.StringVar(value=str(self.app_config.get("num_ctx", 16384)))
        ttk.Entry(row1, textvariable=num_ctx_var, width=14).pack(side=tk.LEFT)
        ttk.Label(row1, text="  (context window, tokens)",
                  foreground="gray").pack(side=tk.LEFT)

        row2 = ttk.Frame(dlg); row2.pack(fill="x", **pad)
        ttk.Label(row2, text="Max history:", width=18).pack(side=tk.LEFT)
        max_hist_var = tk.StringVar(
            value=str(self.app_config.get("max_history_messages", 20)))
        ttk.Entry(row2, textvariable=max_hist_var, width=14).pack(side=tk.LEFT)
        ttk.Label(row2, text="  (messages sent to model)",
                  foreground="gray").pack(side=tk.LEFT)

        row3 = ttk.Frame(dlg); row3.pack(fill="x", **pad)
        ttk.Label(row3, text="Temperature:", width=18).pack(side=tk.LEFT)
        temp_var = tk.StringVar(value=str(self.app_config.get("temperature", 0.2)))
        ttk.Entry(row3, textvariable=temp_var, width=14).pack(side=tk.LEFT)
        ttk.Label(row3, text="  (0.0 – 2.0)", foreground="gray").pack(side=tk.LEFT)

        row4 = ttk.Frame(dlg); row4.pack(fill="x", **pad)
        ttk.Label(row4, text="top_p:", width=18).pack(side=tk.LEFT)
        top_p_var = tk.StringVar(value=str(self.app_config.get("top_p", 0.9)))
        ttk.Entry(row4, textvariable=top_p_var, width=14).pack(side=tk.LEFT)
        ttk.Label(row4, text="  (0.0 – 1.0)", foreground="gray").pack(side=tk.LEFT)

        def _save():
            try:
                nc = int(num_ctx_var.get().strip())
                if nc < 512 or nc > 131072:
                    raise ValueError("num_ctx must be 512 – 131072")
                mh = int(max_hist_var.get().strip())
                if mh < 2 or mh > 200:
                    raise ValueError("Max history must be 2 – 200")
                tp = float(temp_var.get().strip())
                if tp < 0.0 or tp > 2.0:
                    raise ValueError("Temperature must be 0.0 – 2.0")
                tpv = float(top_p_var.get().strip())
                if tpv < 0.0 or tpv > 1.0:
                    raise ValueError("top_p must be 0.0 – 1.0")
            except ValueError as e:
                messagebox.showerror("Settings", str(e), parent=dlg)
                return
            self.app_config["num_ctx"] = nc
            self.app_config["max_history_messages"] = mh
            self.app_config["temperature"] = tp
            self.app_config["top_p"] = tpv
            _save_app_config(self.app_config)
            self._update_ctx_label()
            self.flash_status("Status: Settings saved", "green")
            dlg.destroy()

        btn_row = ttk.Frame(dlg); btn_row.pack(fill="x", padx=10, pady=(10, 10))
        ttk.Button(btn_row, text="Cancel", command=dlg.destroy).pack(side=tk.RIGHT)
        ttk.Button(btn_row, text="Save", command=_save).pack(side=tk.RIGHT, padx=(0, 6))

    def _preview_system_prompt(self):
        persona_key = self.current_persona.get()
        sp = "" if persona_key == NO_PERSONA else self.personalities.get(persona_key, "")
        sp += _calendar_context()
        pending = ""
        if not self.chat_panel.has_placeholder:
            pending = self.chat_panel.user_input_field.get("1.0", tk.END).strip()
        if pending:
            rules = _code_output_rules(pending)
            if rules:
                sp += rules
        body = sp if sp.strip() else "(empty — no persona and no upcoming events)"
        self.chat_panel.append_to_terminal("System Prompt Preview", body)

    def _on_notifier_config_changed(self):
        self.notifier_config["ntfy_topic"] = self.sidebar.ntfy_topic_var.get().strip()
        self.notifier_config["enabled"] = bool(self.sidebar.reminder_enabled_var.get())

        lead_str = self.sidebar.lead_minutes_var.get().strip()
        parsed = []
        for tok in lead_str.replace(",", " ").split():
            if not tok:
                continue
            try:
                v = int(tok)
                if 0 < v <= 1440:
                    parsed.append(v)
            except ValueError:
                pass
        if parsed:
            self.notifier_config["lead_minutes"] = sorted(set(parsed))

        self.reminder_service.reset_notified()

        if self._notifier_save_id is not None:
            try:
                self.root.after_cancel(self._notifier_save_id)
            except Exception:
                pass
        self._notifier_save_id = self.root.after(400, self._save_notifier_config_debounced)

    def _save_notifier_config_debounced(self):
        self._notifier_save_id = None
        _save_notifier_config(self.notifier_config)

    def _test_notification(self):
        topic = self.sidebar.ntfy_topic_var.get().strip()
        if not topic:
            messagebox.showwarning("ntfy", "Enter a topic name first.")
            return
        ok = _send_ntfy(topic, "🔔 Test reminder",
                        "If you see this on your phone, reminders work.")
        if ok:
            self.flash_status("🔔 Test notification sent — check your phone",
                              "green", 4000)
        else:
            self.flash_status("⚠️ Test failed — check topic name and network",
                              "#c33", 5000)

    def _show_ntfy_help(self):
        leads = self.notifier_config.get("lead_minutes", [15, 60])
        leads_str = ", ".join(f"{x} min" for x in leads)
        messagebox.showinfo(
            "Set up phone reminders with ntfy",
            "1. Install the free ntfy app on your phone (Android: Play Store, iOS: App Store).\n\n"
            "2. In the app, tap + and subscribe to a topic name — use something long and unique, "
            "e.g. 'alex-calendar-7f3a91c4'. The topic name acts as a password, so don't use 'calendar'.\n\n"
            "3. Type the same topic name in the box here and tick 'Enable reminders'.\n\n"
            "4. Each event and task has its own reminder time (default 30 min before). "
            "Set it in the event/task dialog, or ask the AI to include reminder_minutes.\n\n"
            f"Legacy events without an explicit reminder fall back to these global lead times: "
            f"{leads_str} before the event.\n\n"
            "5. Click Test. You should get a push notification on your phone within a couple of seconds.\n\n"
            "Self-hosted ntfy? Set the NTFY_URL environment variable (e.g. https://ntfy.example.com/)."
        )

    # --- Models -----------------------------------------------------------
    def _enable_send(self):
        self.chat_panel.set_send_enabled(True)

    def _disable_send_no_ollama(self):
        self.chat_panel.set_send_enabled(False)

    def load_and_sync_models(self):
        if "allow_auto_pull" not in self.app_config:
            allow = messagebox.askyesno(
                "Auto-download Ollama models",
                "This app can download missing Ollama models automatically.\n\n"
                "First-time downloads can be several gigabytes.\n\n"
                "Allow auto-download?\n\n"
                "(You can change this later in app_config.json.)")
            self.app_config["allow_auto_pull"] = bool(allow)
            _save_app_config(self.app_config)
        allow_pull = bool(self.app_config.get("allow_auto_pull", False))

        self.sidebar.set_loading_message("Checking models...")
        self.chat_panel.start_spinner("Syncing models")

        def background_sync():
            try:
                if not os.path.exists(MODELS_FILE):
                    with open(MODELS_FILE, "w", encoding="utf-8") as f:
                        f.write("llama3.2:1b\nqwen2.5:1.5b\n")
                with open(MODELS_FILE, "r", encoding="utf-8") as f:
                    desired = [l.strip() for l in f if l.strip() and not l.startswith("#")]
                if not desired:
                    desired = ["llama3.2:1b"]

                installed = []
                metadata = {}
                try:
                    r = subprocess.run(["ollama", "list"],
                                       capture_output=True, text=True, check=True)
                    lines = r.stdout.strip().split("\n")
                    if len(lines) > 1:
                        for line in lines[1:]:
                            if not line.strip():
                                continue
                            parts = line.split()
                            if len(parts) >= 5:
                                m_name = parts[0]
                                size = parts[3] + " " + parts[4]
                                installed.append(m_name)
                                try:
                                    r_show = subprocess.run(
                                        ["ollama", "show", m_name],
                                        capture_output=True, text=True, check=True)
                                    params = "?"
                                    for sl in r_show.stdout.split('\n'):
                                        if "parameters" in sl.lower():
                                            p_parts = sl.split()
                                            if len(p_parts) >= 2:
                                                params = p_parts[-1]
                                                break
                                    metadata[m_name] = f"Params: {params} | Size: {size}"
                                except Exception:
                                    metadata[m_name] = f"Size: {size}"
                except FileNotFoundError:
                    self.root.after(0, self.chat_panel.stop_spinner)
                    self.root.after(0, lambda: self.sidebar.set_models(
                        ["(Ollama not installed)"]))
                    self.root.after(0, self._disable_send_no_ollama)
                    self.root.after(0, lambda: self.chat_panel.set_status(
                        "Status: Ollama missing — chat disabled", "red"))
                    self.root.after(0, lambda: messagebox.showerror(
                        "Ollama not found",
                        "Install Ollama from https://ollama.com, then restart this app.\n\n"
                        "After installing, click '🔄 Sync & Reload Models'."))
                    return
                except Exception:
                    pass

                for m in desired:
                    if m not in installed and allow_pull:
                        self.root.after(0, lambda mm=m: self.chat_panel.set_spinner_message(
                            f"Installing {mm}"))
                        try:
                            subprocess.run(["ollama", "pull", m],
                                           check=True, capture_output=True)
                        except Exception as e:
                            print(f"Failed to auto-install {m}: {e}")

                self.root.after(0, self.chat_panel.stop_spinner)
                self.root.after(0, lambda: self.sidebar.set_models(desired, metadata))
                self.root.after(0, lambda: self.chat_panel.set_status("Status: Idle", "gray"))
                self.root.after(0, self._enable_send)
            except Exception as e:
                self.root.after(0, self.chat_panel.stop_spinner)
                self.root.after(0, lambda err=e: self.sidebar.set_models(
                    [f"Error: {err}"]))
                self.root.after(0, lambda: self.chat_panel.set_status(
                    "Status: Sync Error", "red"))

        threading.Thread(target=background_sync, daemon=True).start()

    # --- Ollama helpers ---------------------------------------------------
    def _start_ollama(self):
        try:
            subprocess.Popen(["ollama", "serve"],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            self.flash_status("Ollama server starting...", "orange")
            self.root.after(3000, self._check_ollama_started)
        except Exception as e:
            messagebox.showerror("Error", f"Failed to start Ollama: {e}")

    def _check_ollama_started(self):
        try:
            urllib.request.urlopen("http://localhost:11434/", timeout=1)
            self.flash_status("Ollama is running!", "green")
            self._enable_send()
        except Exception:
            self.root.after(2000, self._check_ollama_started)

    # --- Send / stream ----------------------------------------------------
    def _regenerate_response(self):
        if self.is_streaming or not self.conversation_history:
            return
        if self.current_conversation_id is None:
            return

        if self.conversation_history[-1]["role"] == "assistant":
            last_user = ""
            for m in reversed(self.conversation_history):
                if m["role"] == "user":
                    last_user = m["content"]
                    break
            if not last_user:
                return

            with _db() as conn:
                rows = conn.execute(
                    "SELECT id FROM messages WHERE conversation_id = ? ORDER BY id DESC LIMIT 2",
                    (self.current_conversation_id,)
                ).fetchall()
                for r in rows:
                    conn.execute("DELETE FROM messages WHERE id = ?", (r["id"],))

            while self.conversation_history:
                if self.conversation_history[-1]["role"] == "user":
                    self.conversation_history.pop()
                    break
                self.conversation_history.pop()

        elif self.conversation_history[-1]["role"] == "user":
            last_user = self.conversation_history[-1]["content"]
            with _db() as conn:
                rows = conn.execute(
                    "SELECT id FROM messages WHERE conversation_id = ? ORDER BY id DESC LIMIT 1",
                    (self.current_conversation_id,)
                ).fetchall()
                for r in rows:
                    conn.execute("DELETE FROM messages WHERE id = ?", (r["id"],))
            self.conversation_history.pop()
        else:
            return

        self.chat_panel.clear_terminal()
        for m in self.conversation_history:
            self.chat_panel.render_message(m["role"], m["content"])

        self._send_message(last_user)

    def send_query(self):
        if self.chat_panel.has_placeholder:
            return
        q = self.chat_panel.user_input_field.get("1.0", tk.END).strip()
        if not q:
            self.flash_status("Status: Nothing to send", "orange", 1500)
            return
        if not self.current_model.get():
            self.flash_status("Status: Select a model first", "orange")
            return
        if self.is_streaming:
            self.flash_status("Status: Wait for current response", "orange")
            return
        self.chat_panel._reset_input_field()
        self._send_message(q)


    def _send_message(self, query, display_text=None, sender="You"):
        if self.is_streaming:
            self.flash_status("Status: Wait for current response", "orange")
            return
        model = self.current_model.get()
        persona_key = self.current_persona.get()
        if not query or not model:
            return

        if self.current_conversation_id is None:
            seed = (display_text if display_text is not None else query).split("\n")[0].strip() or "New conversation"
            if len(seed) > 50:
                seed = seed[:50] + "…"
            self.current_conversation_id = _create_conversation(seed)
            self.sidebar.refresh_conversations()

        user_msg_id = None
        try:
            user_msg_id = _add_message(self.current_conversation_id, "user", query)
            self.sidebar.refresh_conversations()
        except Exception as e:
            print(f"DB save user msg error: {e}")

        self.chat_panel.append_to_terminal(
            sender, display_text if display_text is not None else query)
        self.chat_panel.start_timer()

        system_prompt = "" if persona_key == NO_PERSONA else self.personalities.get(persona_key, "")
        system_prompt += _calendar_context()
        system_prompt += _code_output_rules(query)

        messages = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        max_hist = self._max_history()
        messages.extend(self.conversation_history[-max_hist:])
        messages.append({"role": "user", "content": query})

        self._stream_cancel.clear()
        self.is_streaming = True
        self.chat_panel.stop_button.config(state="normal")
        self.chat_panel.send_button.config(state="disabled")
        self.chat_panel.retry_button.config(state="disabled")
        try:
            self.chat_panel.user_input_field.focus_set()
        except Exception:
            pass
        threading.Thread(target=self._run_chat_stream,
                         args=(model, messages, persona_key, query, user_msg_id),
                         daemon=True).start()

    def _cancel_stream(self):
        if not self.is_streaming:
            return
        self._stream_cancel.set()
        self.chat_panel.stop_button.config(state="disabled")
        self.chat_panel.set_status("⏹ Stopping…", "orange")
        resp = self._current_response
        if resp is not None:
            try:
                resp.close()
            except Exception:
                pass

    def _rollback_user_message(self, msg_id):
        if msg_id is None:
            return
        try:
            _delete_message(msg_id)
        except Exception as e:
            print(f"Rollback user message error: {e}")

    def _run_chat_stream(self, model, messages, persona_key, user_message, user_msg_id):
        try:
            temperature = float(self.app_config.get("temperature", 0.2))
        except (TypeError, ValueError):
            temperature = 0.2
        try:
            top_p = float(self.app_config.get("top_p", 0.9))
        except (TypeError, ValueError):
            top_p = 0.9
        try:
            num_ctx = int(self.app_config.get("num_ctx", 4096))
        except (TypeError, ValueError):
            num_ctx = 4096
        max_hist = self._max_history()

        payload = json.dumps({
            "model": model,
            "messages": messages,
            "stream": True,
            "options": {
                "temperature": temperature,
                "top_p": top_p,
                "repeat_penalty": 1.05,
                "num_ctx": num_ctx,
            }
        }).encode("utf-8")
        req = urllib.request.Request(self.OLLAMA_CHAT_URL, data=payload,
                                     headers={"Content-Type": "application/json"})
        accumulated = []
        header_shown = False
        saved_assistant = False
        cancelled = False

        try:
            with urllib.request.urlopen(req, timeout=600) as response:
                self._current_response = response
                for raw_line in response:
                    if self._stream_cancel.is_set():
                        cancelled = True
                        break
                    if not raw_line.strip():
                        continue
                    try:
                        chunk = json.loads(raw_line.decode("utf-8"))
                    except json.JSONDecodeError:
                        continue
                    if not header_shown:
                        self.root.after(0, self.chat_panel.stop_timer)
                        persona_label = "no persona" if persona_key == NO_PERSONA else persona_key
                        self.root.after(0, lambda lbl=f"AI ({model} | {persona_label})":
                                        self.chat_panel.append_ai_header(lbl))
                        header_shown = True
                        self.root.after(0, lambda: self.chat_panel.set_status(
                            "⏳ Streaming…", "orange"))
                    if "message" in chunk:
                        content = chunk["message"].get("content", "")
                        if content:
                            accumulated.append(content)
                            self.root.after(
                                0, lambda c=content: self.chat_panel.stream_chunk_to_terminal(c))
                    if chunk.get("done", False):
                        break

            if cancelled:
                self._rollback_user_message(user_msg_id)
                return

            full_response = "".join(accumulated).strip()

            if not full_response:
                placeholder = "(The model returned an empty response.)"
                self.root.after(0, lambda: self.chat_panel.stream_chunk_to_terminal(
                    f"{placeholder}\n\n"))
                try:
                    _add_message(self.current_conversation_id, "assistant", placeholder)
                    saved_assistant = True
                except Exception as e:
                    print(f"DB save empty response error: {e}")
                self.conversation_history.append({"role": "user", "content": user_message})
                self.conversation_history.append({"role": "assistant", "content": placeholder})
                if len(self.conversation_history) > max_hist:
                    self.conversation_history = self.conversation_history[-max_hist:]
                self.root.after(0, lambda: self.flash_status(
                    "⚠️ Model returned an empty response", "#c33", 4000))
                self.root.after(0, self._update_history_label)
                self.root.after(0, self.sidebar.refresh_conversations)
                return

            self.root.after(0, lambda: self.chat_panel.stream_chunk_to_terminal("\n\n"))
            self.conversation_history.append({"role": "user", "content": user_message})
            self.conversation_history.append({"role": "assistant", "content": full_response})
            if len(self.conversation_history) > max_hist:
                self.conversation_history = self.conversation_history[-max_hist:]
            try:
                _add_message(self.current_conversation_id, "assistant", full_response)
                saved_assistant = True
            except Exception as e:
                print(f"DB save assistant msg error: {e}")
            self.root.after(0, self._update_history_label)
            self.root.after(0, self.sidebar.refresh_conversations)
            self.root.after(0, lambda: self.chat_panel._replace_last_ai_message(full_response))

        except urllib.error.URLError as e:
            if self._stream_cancel.is_set():
                cancelled = True
                self._rollback_user_message(user_msg_id)
            else:
                self._rollback_user_message(user_msg_id)
                self.root.after(0, self.chat_panel.stop_timer)
                self.root.after(0, lambda msg=str(e): self.chat_panel.append_error(
                    f"⚠️ Cannot connect to Ollama ({msg}). Is `ollama serve` running?"))
                self.root.after(0, self.chat_panel.inject_ollama_start_btn)
        except Exception as e:
            if self._stream_cancel.is_set():
                cancelled = True
                self._rollback_user_message(user_msg_id)
            else:
                self._rollback_user_message(user_msg_id)
                self.root.after(0, self.chat_panel.stop_timer)
                self.root.after(0, lambda msg=str(e): self.chat_panel.append_error(
                    f"⚠️ Error: {msg}"))
        finally:
            self._current_response = None
            self.root.after(0, lambda ok=saved_assistant, c=cancelled:
                            self._on_stream_finished(ok, c))

    def _on_stream_finished(self, had_assistant_reply=False, cancelled=False):
        self.is_streaming = False
        self.chat_panel.stop_button.config(state="disabled")
        self.chat_panel.send_button.config(state="normal")
        self.chat_panel.stop_timer()

        if had_assistant_reply or (
                self.conversation_history
                and self.conversation_history[-1]["role"] == "user"):
            self.chat_panel.retry_button.config(state="normal")
        else:
            self.chat_panel.retry_button.config(state="disabled")

        try:
            self.chat_panel.user_input_field.focus_set()
        except Exception:
            pass

        if cancelled:
            self.flash_status("Status: Stopped", "orange", 2000)
            return

        if not had_assistant_reply:
            return

        proposed = self._offer_calendar_suggestions()
        if proposed:
            self.flash_status(
                f"📅📋 {proposed} item{'s' if proposed > 1 else ''} proposed — "
                f"review the cards below",
                "orange", duration=8000)
            return

        last = self.conversation_history[-1].get("content", "") if self.conversation_history else ""
        looks_like_event = (
            '"title"' in last or "'title'" in last or
            '"start"' in last or '"start_ts"' in last or
            '"due_ts"' in last or
            "```calendar" in last.lower() or
            "```task" in last.lower()
        )
        if looks_like_event:
            self.flash_status(
                "⚠️ Model tried to create an item but format didn't parse "
                "(check parse_failures.log)",
                "#c33", duration=6000)
            return

        if len(self.conversation_history) >= 2:
            last_user = self.conversation_history[-2].get("content", "").lower()
            keywords = ("schedule", "book", "add to calendar", "remind",
                        "meeting", "appointment", "reserve",
                        "task", "to-do", "todo", "add to task")
            if any(k in last_user for k in keywords):
                self.flash_status(
                    "⚠️ No calendar/task proposal in AI reply "
                    "(model didn't attempt to schedule anything)",
                    "#c33", duration=4000)
        self.chat_panel._check_scroll_btn()

    def _offer_calendar_suggestions(self):
        if not self.conversation_history:
            return 0
        last = self.conversation_history[-1]
        if last.get("role") != "assistant":
            return 0
        content = last.get("content", "")
        events = _extract_calendar_events(content)
        tasks = _extract_task_blocks(content)
        if events:
            self.chat_panel.render_calendar_suggestions(events)
        if tasks:
            self.chat_panel.render_task_suggestions(tasks)
        return len(events) + len(tasks)

    def _add_suggested_event(self, ev):
        try:
            rm = ev.get("reminder_minutes")
            if rm is None:
                rm = DEFAULT_REMINDER_MINUTES
            with _db() as conn:
                conn.execute(
                    "INSERT INTO events (title, start_ts, end_ts, description, "
                    "reminder_minutes) VALUES (?, ?, ?, ?, ?)",
                    (ev["title"],
                     _parse_ts_relaxed(ev["start_ts"]) or ev["start_ts"],
                     _parse_ts_relaxed(ev["end_ts"]) or ev["end_ts"],
                     ev.get("description", ""), int(rm)))
            self.calendar_tab.refresh()
            self.flash_status(f"Status: Added '{ev['title']}' to calendar", "green")
            rm_note = f", 🔔 {int(rm)}m before" if int(rm) > 0 else ", 🔕 off"
            note = (f"[System note: Event confirmed and saved — "
                    f"'{ev['title']}' at {ev['start_ts']} to {ev['end_ts']}{rm_note}]")
            self.conversation_history.append({"role": "assistant", "content": note})
            try:
                _add_message(self.current_conversation_id, "assistant", note)
            except Exception as e:
                print(f"DB save system note error: {e}")
        except Exception as e:
            self.flash_status(f"Status: Failed to add event: {e}", "red")

    def _add_suggested_task(self, t):
        try:
            rm = t.get("reminder_minutes")
            if rm is None:
                rm = DEFAULT_REMINDER_MINUTES
            _add_task(t["title"], t.get("description", ""),
                      t.get("due_ts"), t.get("priority", "normal"), int(rm))
            self.tasks_tab.refresh()
            self.flash_status(f"Status: Added task '{t['title']}'", "green")
            rm_note = f", 🔔 {int(rm)}m before" if int(rm) > 0 else ", 🔕 off"
            note = (f"[System note: Task confirmed and saved — "
                    f"'{t['title']}'"
                    + (f" (due: {t['due_ts']})" if t.get('due_ts') else "")
                    + rm_note + "]")
            self.conversation_history.append(
                {"role": "assistant", "content": note})
            try:
                _add_message(self.current_conversation_id, "assistant", note)
            except Exception as e:
                print(f"DB save system note error: {e}")
        except Exception as e:
            self.flash_status(f"Status: Failed to add task: {e}", "red")


if __name__ == "__main__":
    root = tk.Tk()
    app = LocalAIApp(root)
    root.mainloop()