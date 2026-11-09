"""Chat tab: status bar, message display, input area, streaming markdown."""
import re
import time
import json
import tkinter as tk
import webbrowser
import os
from tkinter import ttk, messagebox, scrolledtext, filedialog

from core.parsing import (
    _normalize_json_text, _strip_json_comments, _log_parse_failure,
)

class ChatPanel(ttk.Frame):
    def __init__(self, parent, app):
        super().__init__(parent, padding=12)
        self.app = app
        self.is_thinking = False
        self.start_time = 0
        self.timer_after_id = None
        self._flash_after = None
        self._spinner_active = False
        self._spinner_after = None
        self._spinner_idx = 0
        self._spinner_message = "Working"
        self._spinner_frames = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]
        self._current_ai_start_index = None
        self.has_placeholder = True
        self.placeholder_text = "Type your prompt here... (Enter to send, Shift+Enter for newline)"

        self.attached_image_path = None
        self.prompts = {}

        self._build()
        self.apply_font_size(self.app.app_config.get("chat_font_size", 10))
        self.load_prompts()

    def load_prompts(self):
        from core.db import PROMPTS_FILE
        self.prompts = {}
        if os.path.exists(PROMPTS_FILE):
            try:
                with open(PROMPTS_FILE, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if line and not line.startswith("#") and "=" in line:
                            k, v = line.split("=", 1)
                            self.prompts[k.strip()] = v.strip()
            except Exception as e:
                print(f"Error loading prompts: {e}")

        if hasattr(self, "prompt_combo"):
            self.prompt_combo["values"] = [""] + list(self.prompts.keys())

    def _build(self):
        self.rowconfigure(2, weight=1)
        self.columnconfigure(0, weight=1)
        ttk.Label(self, text="💻 Dev & Business Terminal", font=("Segoe UI", 11, "bold")).grid(row=0, column=0, sticky="w", pady=(0, 2))
        self.status_label = ttk.Label(self, text="Status: Idle", font=("Segoe UI", 9, "italic"), foreground=self.app.theme["fg_muted"])
        self.status_label.grid(row=1, column=0, sticky="w", pady=(0, 4))

        self.chat_display = scrolledtext.ScrolledText(self, wrap=tk.WORD, bg=self.app.theme["bg_main"], fg=self.app.theme["fg_main"], font=("Consolas", 10), insertbackground="white")
        self.chat_display.grid(row=2, column=0, sticky="nsew", pady=(0, 10))

        self.chat_display.tag_config("user_header", foreground=self.app.theme["accent_teal"], font=("Segoe UI", 10, "bold"), lmargin1=5, lmargin2=5, spacing1=6)
        self.chat_display.tag_config("user_body", foreground=self.app.theme["fg_main"], background=self.app.theme["bg_panel"], font=("Consolas", 10), lmargin1=10, lmargin2=10, rmargin=10, spacing3=6)
        self.chat_display.tag_config("ai_header", foreground=self.app.theme["accent_purple"], font=("Segoe UI", 10, "bold"), lmargin1=5, lmargin2=5, spacing1=6)
        self.chat_display.tag_config("ai_body", foreground=self.app.theme["fg_main"], background=self.app.theme["bg_main"], font=("Consolas", 10), lmargin1=10, lmargin2=10, rmargin=10, spacing3=6)
        self.chat_display.tag_config("error_body", foreground=self.app.theme["accent_red"], font=("Consolas", 10), lmargin1=10, lmargin2=10, spacing3=4)
        self.chat_display.tag_config("sel", background="#264F78", foreground="#ffffff")
        self.chat_display.tag_raise("sel")
        self.chat_display.tag_config("code_block", foreground=self.app.theme["fg_main"], background=self.app.theme["bg_card"], font=("Consolas", 10), lmargin1=20, lmargin2=20, spacing1=2, spacing3=6)
        self.chat_display.tag_config("bold", font=("Consolas", 10, "bold"))
        self.chat_display.tag_config("inline_code", foreground="#ce9178", background=self.app.theme["bg_card"], font=("Consolas", 9))

        self.chat_display.tag_config("link", foreground=self.app.theme["accent_teal"], underline=True)
        self.chat_display.tag_bind("link", "<Button-1>", self._open_link)

        self.chat_display.config(state=tk.DISABLED)
        self.chat_display.bind("<Control-c>", self._copy_selection)
        self.chat_display.bind("<Control-C>", self._copy_selection)
        self.chat_display.bind("<Control-a>", self._select_all_terminal)
        self.chat_display.bind("<Control-A>", self._select_all_terminal)
        self.chat_display.bind("<Button-3>", self._show_terminal_context_menu)
        self.chat_display.bind("<Button-2>", self._show_terminal_context_menu)
        self.chat_display.bind("<Control-Button-1>", self._show_terminal_context_menu)

        self.scroll_btn = ttk.Button(self, text="⬇", command=self._scroll_to_bottom, width=3)
        self.chat_display.bind("<Configure>", lambda e: self._check_scroll_btn())
        self.chat_display.bind("<MouseWheel>", lambda e: self.after(10, self._check_scroll_btn))
        self.chat_display.bind("<Button-4>", lambda e: self.after(10, self._check_scroll_btn))
        self.chat_display.bind("<Button-5>", lambda e: self.after(10, self._check_scroll_btn))
        self.chat_display.bind("<KeyRelease>", lambda e: self._check_scroll_btn())

        input_container = ttk.Frame(self)
        input_container.grid(row=3, column=0, sticky="ew")
        input_container.columnconfigure(0, weight=1)

        top_bar = ttk.Frame(input_container)
        top_bar.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 4))

        ttk.Label(top_bar, text="Prompt Library:").pack(side=tk.LEFT)
        self.prompt_combo = ttk.Combobox(top_bar, state="readonly", width=30)
        self.prompt_combo.pack(side=tk.LEFT, padx=(5, 15))
        self.prompt_combo.bind("<<ComboboxSelected>>", self._on_prompt_selected)

        self.attach_btn = ttk.Button(top_bar, text="📎 Attach Image", command=self._attach_image)
        self.attach_btn.pack(side=tk.LEFT)

        self.image_label = ttk.Label(top_bar, text="", foreground=self.app.theme["accent_teal"])
        self.image_label.pack(side=tk.LEFT, padx=(5, 0))

        ttk.Button(top_bar, text="✕ Clear Image", command=self._clear_image).pack(side=tk.LEFT, padx=(5, 0))

        input_subframe = ttk.Frame(input_container)
        input_subframe.grid(row=1, column=0, sticky="ew")
        input_subframe.columnconfigure(0, weight=1)

        self.user_input_field = tk.Text(input_subframe, height=4, font=("Consolas", 10), wrap=tk.WORD, bg=self.app.theme["bg_main"], fg=self.app.theme["fg_main"], insertbackground="white")
        self.user_input_field.grid(row=0, column=0, sticky="nsew", padx=(0, 8))
        self.user_input_field.insert("1.0", self.placeholder_text)
        self.user_input_field.config(fg=self.app.theme["fg_muted"])
        self.user_input_field.bind("<FocusIn>", self.on_focus_in)
        self.user_input_field.bind("<FocusOut>", self.on_focus_out)
        self.user_input_field.bind("<Return>", self.handle_return_key)
        self.user_input_field.bind("<Shift-Return>", lambda event: None)
        self.user_input_field.tag_config("keyword", foreground="#569cd6")
        self.user_input_field.tag_config("string", foreground="#ce9178")
        self.user_input_field.bind("<<Modified>>", self._on_input_modified)

        btn_frame = ttk.Frame(input_subframe)
        btn_frame.grid(row=0, column=1, sticky="ne")
        self.send_button = ttk.Button(btn_frame, text="Execute 🚀", command=self.app.send_query)
        self.send_button.pack(side=tk.TOP, fill="x", pady=(0, 4))
        self.stop_button = ttk.Button(btn_frame, text="Stop ⏹", command=self.app._cancel_stream, state="disabled")
        self.stop_button.pack(side=tk.TOP, fill="x", pady=(0, 4))
        self.retry_button = ttk.Button(btn_frame, text="Retry 🔄", command=self.app._regenerate_response, state="disabled")
        self.retry_button.pack(side=tk.TOP, fill="x")

    def _on_prompt_selected(self, event=None):
        selected = self.prompt_combo.get()
        if not selected: return
        prompt_text = self.prompts.get(selected, "")
        if prompt_text:
            self.user_input_field.delete("1.0", tk.END)
            self.user_input_field.insert("1.0", prompt_text + "\n\n")
            self.user_input_field.config(fg=self.app.theme["fg_main"])
            self.has_placeholder = False
            self.user_input_field.focus_set()
        self.prompt_combo.set("")

    def _attach_image(self):
        filepath = filedialog.askopenfilename(
            parent=self.winfo_toplevel(),
            filetypes=[("Images", "*.png *.jpg *.jpeg *.webp *.bmp"), ("All Files", "*.*")]
        )
        if filepath:
            self.attached_image_path = filepath
            self.image_label.config(text=f"Attached: {os.path.basename(filepath)}")

    def _clear_image(self):
        self.attached_image_path = None
        self.image_label.config(text="")

    def _open_link(self, event):
        try:
            idx = self.chat_display.index(f"@{event.x},{event.y}")
            start = self.chat_display.index(f"{idx} linestart")
            end = self.chat_display.index(f"{idx} lineend")
            line = self.chat_display.get(start, end)
            m = re.search(r'(https?://\S+)', line)
            if m: webbrowser.open(m.group(1))
        except Exception: pass

    def apply_font_size(self, size):
        size = int(size)
        self.chat_display.tag_config("user_header", font=("Segoe UI", size, "bold"))
        self.chat_display.tag_config("user_body", font=("Consolas", size))
        self.chat_display.tag_config("ai_header", font=("Segoe UI", size, "bold"))
        self.chat_display.tag_config("ai_body", font=("Consolas", size))
        self.chat_display.tag_config("error_body", font=("Consolas", size))
        self.chat_display.tag_config("code_block", font=("Consolas", size))
        self.chat_display.tag_config("bold", font=("Consolas", size, "bold"))
        self.chat_display.tag_config("inline_code", font=("Consolas", size - 1))
        self.user_input_field.config(font=("Consolas", size))

    def _check_scroll_btn(self):
        try:
            y_view = self.chat_display.yview()
            if y_view[1] < 0.95: self.scroll_btn.place(in_=self.chat_display, relx=0.95, rely=0.95, anchor="se")
            else: self.scroll_btn.place_forget()
        except Exception: pass

    def _scroll_to_bottom(self):
        self.chat_display.see(tk.END)
        self._check_scroll_btn()

    def _copy_code(self, code):
        top = self.winfo_toplevel()
        top.clipboard_clear()
        top.clipboard_append(code)
        self.flash_status("Status: Code copied to clipboard", "green", 2000)

    def _insert_markdown(self, text, sender="AI"):
        self.chat_display.config(state=tk.NORMAL)
        if sender: self.chat_display.insert(tk.END, f"\n[{sender}]\n", "ai_header")

        parts = re.split(r'(```.*?\n.*?```)', text, flags=re.DOTALL)
        for part in parts:
            if part.startswith("```") and part.endswith("```"):
                lines = part.split('\n')
                code = '\n'.join(lines[1:-1])
                btn_frame = tk.Frame(self.chat_display, bg=self.app.theme["bg_main"])
                copy_btn = tk.Button(btn_frame, text="📋 Copy Code", command=lambda c=code: self._copy_code(c), bg=self.app.theme["border"], fg=self.app.theme["fg_main"], activebackground=self.app.theme["bg_panel"], relief="flat", font=("Segoe UI", 8), bd=0, padx=5, pady=2)
                copy_btn.pack(side=tk.RIGHT, padx=5, pady=2)
                self.chat_display.insert(tk.END, "\n", "ai_body")
                self.chat_display.window_create(tk.END, window=btn_frame)
                self.chat_display.insert(tk.END, "\n", "ai_body")
                self.chat_display.insert(tk.END, code + "\n\n", "code_block")
            else:
                inline_parts = re.split(r'(\*\*.*?\*\*|`.*?`|https?://\S+)', part)
                for ip in inline_parts:
                    if not ip: continue
                    if ip.startswith("**") and ip.endswith("**"):
                        self.chat_display.insert(tk.END, ip[2:-2], ("ai_body", "bold"))
                    elif ip.startswith("`") and ip.endswith("`"):
                        self.chat_display.insert(tk.END, ip[1:-1], ("ai_body", "inline_code"))
                    elif ip.startswith("http"):
                        self.chat_display.insert(tk.END, ip, ("ai_body", "link"))
                    else:
                        self.chat_display.insert(tk.END, ip, "ai_body")

        self.chat_display.config(state=tk.DISABLED)
        self._check_scroll_btn()

    def _replace_last_ai_message(self, full_response):
        self.chat_display.config(state=tk.NORMAL)
        if self._current_ai_start_index:
            self.chat_display.delete(self._current_ai_start_index, tk.END)
            self.chat_display.config(state=tk.DISABLED)
            self._insert_markdown(full_response, sender=None)
        else: self.chat_display.config(state=tk.DISABLED)
        self._check_scroll_btn()

    def _on_input_modified(self, event):
        if self.user_input_field.edit_modified():
            self._highlight_input()
            self.user_input_field.edit_modified(False)

    def _highlight_input(self):
        if self.has_placeholder: return
        text = self.user_input_field.get("1.0", "end-1c")
        self.user_input_field.tag_remove("keyword", "1.0", tk.END)
        self.user_input_field.tag_remove("string", "1.0", tk.END)
        for m in re.finditer(r'("[^"]*"|\'[^\']*\')', text):
            self.user_input_field.tag_add("string", f"1.0+{m.start()}c", f"1.0+{m.end()}c")
        keywords = (r'\b(def|class|import|from|return|if|else|elif|for|while|try|except|'
                    r'with|as|pass|break|continue|in|is|not|and|or|None|True|False)\b')
        for m in re.finditer(keywords, text):
            self.user_input_field.tag_add("keyword", f"1.0+{m.start()}c", f"1.0+{m.end()}c")

    def on_focus_in(self, event):
        if self.has_placeholder:
            self.user_input_field.delete("1.0", tk.END)
            self.user_input_field.config(fg=self.app.theme["fg_main"])
            self.has_placeholder = False

    def on_focus_out(self, event):
        if not self.user_input_field.get("1.0", tk.END).strip():
            self.user_input_field.insert("1.0", self.placeholder_text)
            self.user_input_field.config(fg=self.app.theme["fg_muted"])
            self.has_placeholder = True

    def handle_return_key(self, event):
        if event.state & 0x1: return None
        self.app.send_query()
        return "break"

    def _reset_input_field(self):
        self.user_input_field.delete("1.0", tk.END)
        self.user_input_field.insert("1.0", self.placeholder_text)
        self.user_input_field.config(fg=self.app.theme["fg_muted"])
        self.has_placeholder = True
        self._clear_image()

    def set_status(self, text, color="gray"):
        try: self.status_label.config(text=text, foreground=color)
        except Exception: pass

    def flash_status(self, message, color="green", duration=2500):
        if self._flash_after is not None:
            try: self.after_cancel(self._flash_after)
            except Exception: pass
        self.status_label.config(text=message, foreground=color)

        def _reset():
            self._flash_after = None
            if not self.app.is_streaming and not self.is_thinking and not self._spinner_active:
                self.status_label.config(text="Status: Idle", foreground=self.app.theme["fg_muted"])
        self._flash_after = self.after(duration, _reset)

    def _cancel_spinner_loop(self):
        self._spinner_active = False
        if self._spinner_after:
            try: self.after_cancel(self._spinner_after)
            except Exception: pass
            self._spinner_after = None

    def _cancel_timer_loop(self):
        self.is_thinking = False
        if self.timer_after_id:
            try: self.after_cancel(self.timer_after_id)
            except Exception: pass
            self.timer_after_id = None

    def start_spinner(self, message="Working"):
        self._cancel_timer_loop()
        self._cancel_spinner_loop()
        self._spinner_message = message
        self._spinner_idx = 0
        self._spinner_active = True
        self._animate_spinner()

    def _animate_spinner(self):
        if not self._spinner_active: return
        f = self._spinner_frames[self._spinner_idx % len(self._spinner_frames)]
        self._spinner_idx += 1
        self.status_label.config(text=f"{f} {self._spinner_message}…", foreground="orange")
        self._spinner_after = self.after(100, self._animate_spinner)

    def stop_spinner(self):
        self._cancel_spinner_loop()
        if not self.app.is_streaming and not self.is_thinking:
            try: self.status_label.config(text="Status: Idle", foreground=self.app.theme["fg_muted"])
            except Exception: pass

    def set_spinner_message(self, message):
        self._spinner_message = message

    def start_timer(self):
        self._cancel_spinner_loop()
        self._cancel_timer_loop()
        self.is_thinking = True
        self.start_time = time.time()
        self.update_timer()

    def update_timer(self):
        if self.is_thinking:
            elapsed = int(time.time() - self.start_time)
            self.status_label.config(text=f"🧠 Thinking... ({elapsed}s elapsed)", foreground="orange")
            self.timer_after_id = self.after(1000, self.update_timer)

    def stop_timer(self):
        self._cancel_timer_loop()
        if not self.app.is_streaming:
            try: self.status_label.config(text="Status: Idle", foreground=self.app.theme["fg_muted"])
            except Exception: pass

    def set_send_enabled(self, enabled):
        state = "normal" if enabled else "disabled"
        try: self.send_button.config(state=state)
        except Exception: pass

    def clear_terminal(self):
        self.chat_display.config(state=tk.NORMAL)
        self.chat_display.delete("1.0", tk.END)
        self.chat_display.config(state=tk.DISABLED)

    def _clear_terminal_display(self):
        self.clear_terminal()
        self.flash_status("Status: Terminal cleared (memory retained)", "green")

    def append_to_terminal(self, sender, text):
        self.chat_display.config(state=tk.NORMAL)
        self.chat_display.insert(tk.END, f"\n[{sender}]\n", "user_header")
        self.chat_display.insert(tk.END, f"{text}\n\n", "user_body")
        self.chat_display.see(tk.END)
        self.chat_display.config(state=tk.DISABLED)
        self._check_scroll_btn()

    def append_ai_header(self, sender):
        self.chat_display.config(state=tk.NORMAL)
        self.chat_display.insert(tk.END, f"\n[{sender}]\n", "ai_header")
        self.chat_display.see(tk.END)
        self.chat_display.config(state=tk.DISABLED)
        self._current_ai_start_index = self.chat_display.index(tk.END)

    def append_error(self, message):
        self.chat_display.config(state=tk.NORMAL)
        self.chat_display.insert(tk.END, f"{message}\n", "error_body")
        self.chat_display.see(tk.END)
        self.chat_display.config(state=tk.DISABLED)
        self._check_scroll_btn()

    def stream_chunk_to_terminal(self, text_chunk):
        if not text_chunk: return
        self.chat_display.config(state=tk.NORMAL)
        y_view = self.chat_display.yview()
        is_bottom = y_view[1] >= 0.95
        self.chat_display.insert(tk.END, text_chunk, "ai_body")
        if is_bottom: self.chat_display.see(tk.END)
        self.chat_display.config(state=tk.DISABLED)
        self._check_scroll_btn()

    def render_message(self, role, content):
        if role == "user": self.append_to_terminal("You", content)
        elif role == "assistant":
            cleaned = self._summarize_calendar_blocks(content)
            self._insert_markdown(cleaned, sender="AI")

    def _summarize_calendar_blocks(self, content):
        def _parse_block(raw):
            block = _normalize_json_text(raw.strip())
            block = _strip_json_comments(block)
            if block.lower().startswith("json"):
                block = block[4:].lstrip()
            try:
                return json.loads(block)
            except (json.JSONDecodeError, ValueError):
                s = block.find("{")
                e = block.rfind("}")
                if s >= 0 and e > s:
                    try:
                        return json.loads(block[s:e+1])
                    except (json.JSONDecodeError, ValueError):
                        pass
            return None

        def _ev_summary(ev):
            if not isinstance(ev, dict):
                return None
            rm = ev.get("reminder_minutes")
            extra = f" · 🔔 {rm}m" if isinstance(rm, int) and rm > 0 else ""
            return f"📅 [Event proposed: {ev.get('title','?')} at {ev.get('start_ts','?')}{extra}]"

        def _task_summary(t):
            if not isinstance(t, dict):
                return None
            rm = t.get("reminder_minutes")
            extra = f" · 🔔 {rm}m" if isinstance(rm, int) and rm > 0 else ""
            return f"📋 [Task proposed: {t.get('title','?')}{extra}]"

        def cal_replacer(match):
            data = _parse_block(match.group(1))
            if isinstance(data, list):
                lines = [x for x in (_ev_summary(ev) for ev in data) if x]
                if lines:
                    return "\n" + "\n".join(lines) + "\n"
            s = _ev_summary(data)
            if s:
                return "\n" + s + "\n"
            return "\n📅 [Event proposed]\n"

        def task_replacer(match):
            data = _parse_block(match.group(1))
            if isinstance(data, list):
                lines = [x for x in (_task_summary(t) for t in data) if x]
                if lines:
                    return "\n" + "\n".join(lines) + "\n"
            s = _task_summary(data)
            if s:
                return "\n" + s + "\n"
            return "\n📋 [Task proposed]\n"

        def batch_replacer(match):
            return "\n🛠 [Batch tasks proposed — see below]\n"

        content = re.sub(r"```\s*calendar\b[^\n]*\n?(.*?)```", cal_replacer, content, flags=re.DOTALL | re.IGNORECASE)
        content = re.sub(r"```\s*task\b[^\n]*\n?(.*?)```", task_replacer, content, flags=re.DOTALL | re.IGNORECASE)
        content = re.sub(r"```\s*batch\b[^\n]*\n?(.*?)```", batch_replacer, content, flags=re.DOTALL | re.IGNORECASE)
        return content.strip()

    def render_calendar_suggestions(self, events):
        self.chat_display.config(state=tk.NORMAL)
        container = tk.Frame(self.chat_display, bg=self.app.theme["bg_main"])
        for ev in events:
            card = tk.Frame(container, bg=self.app.theme["bg_panel"], highlightthickness=2, highlightbackground=self.app.theme["accent_blue"], padx=10, pady=10)
            card.pack(fill="x", pady=6, padx=8)
            tk.Label(card, text=f"📅 {ev['title']}", bg=self.app.theme["bg_panel"], fg=self.app.theme["accent_teal"], font=("Segoe UI", 10, "bold"), anchor="w").pack(fill="x")
            tk.Label(card, text=f"{ev['start_ts']} → {ev['end_ts']}", bg=self.app.theme["bg_panel"], fg=self.app.theme["fg_main"], font=("Consolas", 9), anchor="w").pack(fill="x", pady=(2, 0))
            rm = ev.get("reminder_minutes")
            if rm is not None:
                rm_txt = f"🔔 Reminder: {rm} min before" if rm > 0 else "🔕 Reminder: off"
                tk.Label(card, text=rm_txt, bg=self.app.theme["bg_panel"], fg=self.app.theme["accent_teal"] if rm > 0 else self.app.theme["fg_muted"], font=("Segoe UI", 9), anchor="w").pack(fill="x", pady=(2, 0))
            if ev.get("description"):
                tk.Label(card, text=ev["description"], bg=self.app.theme["bg_panel"], fg=self.app.theme["fg_muted"], font=("Segoe UI", 9), anchor="w", wraplength=500, justify="left").pack(fill="x", pady=(2, 0))
            btn = tk.Button(card, text="  ➕  Add to Calendar  ", command=lambda e=ev: self.app._add_suggested_event(e), bg=self.app.theme["accent_blue"], fg="white", activebackground=self.app.theme["border"], activeforeground="white", font=("Segoe UI", 10, "bold"), relief="flat", cursor="hand2", padx=12, pady=6)
            btn.pack(anchor="e", pady=(10, 0))
        self.chat_display.window_create(tk.END, window=container)
        self.chat_display.insert(tk.END, "\n\n")
        self.chat_display.see(tk.END)
        self.chat_display.config(state=tk.DISABLED)
        self._check_scroll_btn()

    def render_task_suggestions(self, tasks):
        self.chat_display.config(state=tk.NORMAL)
        container = tk.Frame(self.chat_display, bg=self.app.theme["bg_main"])
        for t in tasks:
            card = tk.Frame(container, bg=self.app.theme["bg_panel"], highlightthickness=2, highlightbackground=self.app.theme["accent_purple"], padx=10, pady=10)
            card.pack(fill="x", pady=6, padx=8)
            tk.Label(card, text=f"📋 {t['title']}", bg=self.app.theme["bg_panel"], fg=self.app.theme["accent_purple"], font=("Segoe UI", 10, "bold"), anchor="w").pack(fill="x")
            info = []
            if t.get("priority") and t["priority"] != "normal": info.append(f"Priority: {t['priority']}")
            if t.get("due_ts"): info.append(f"Due: {t['due_ts']}")
            if info: tk.Label(card, text="  ·  ".join(info), bg=self.app.theme["bg_panel"], fg=self.app.theme["fg_main"], font=("Consolas", 9), anchor="w").pack(fill="x", pady=(2, 0))
            rm = t.get("reminder_minutes")
            if rm is not None:
                rm_txt = f"🔔 Reminder: {rm} min before due" if rm > 0 else "🔕 Reminder: off"
                tk.Label(card, text=rm_txt, bg=self.app.theme["bg_panel"], fg=self.app.theme["accent_teal"] if rm > 0 else self.app.theme["fg_muted"], font=("Segoe UI", 9), anchor="w").pack(fill="x", pady=(2, 0))
            if t.get("description"):
                tk.Label(card, text=t["description"], bg=self.app.theme["bg_panel"], fg=self.app.theme["fg_muted"], font=("Segoe UI", 9), anchor="w", wraplength=500, justify="left").pack(fill="x", pady=(2, 0))
            btn = tk.Button(card, text="  ➕  Add to Tasks  ", command=lambda task=t: self.app._add_suggested_task(task), bg=self.app.theme["accent_purple"], fg="white", activebackground=self.app.theme["border"], activeforeground="white", font=("Segoe UI", 10, "bold"), relief="flat", cursor="hand2", padx=12, pady=6)
            btn.pack(anchor="e", pady=(10, 0))
        self.chat_display.window_create(tk.END, window=container)
        self.chat_display.insert(tk.END, "\n\n")
        self.chat_display.see(tk.END)
        self.chat_display.config(state=tk.DISABLED)
        self._check_scroll_btn()

    def render_batch_suggestions(self, tasks):
        self.chat_display.config(state=tk.NORMAL)
        container = tk.Frame(self.chat_display, bg=self.app.theme["bg_main"])
        
        card = tk.Frame(container, bg=self.app.theme["bg_panel"], highlightthickness=2, highlightbackground="#569cd6", padx=10, pady=10)
        card.pack(fill="x", pady=6, padx=8)
        
        tk.Label(card, text="🛠 Batch Tasks Proposed", bg=self.app.theme["bg_panel"], fg="#569cd6", font=("Segoe UI", 10, "bold"), anchor="w").pack(fill="x")
        
        for t in tasks:
            tk.Label(card, text=f"• {t['file']}: {t['task']}", bg=self.app.theme["bg_panel"], fg=self.app.theme["fg_main"], font=("Consolas", 9), anchor="w", wraplength=500, justify="left").pack(fill="x", pady=(4, 0))
            
        btn = tk.Button(card, text="  ➕  Send to Batch Tab  ", command=lambda ts=tasks: self.app._add_batch_tasks(ts), bg="#569cd6", fg="white", activebackground=self.app.theme["border"], activeforeground="white", font=("Segoe UI", 10, "bold"), relief="flat", cursor="hand2", padx=12, pady=6)
        btn.pack(anchor="e", pady=(10, 0))
        
        self.chat_display.window_create(tk.END, window=container)
        self.chat_display.insert(tk.END, "\n\n")
        self.chat_display.see(tk.END)
        self.chat_display.config(state=tk.DISABLED)
        self._check_scroll_btn()

    def _copy_selection(self, event=None):
        try: selection = self.chat_display.get("sel.first", "sel.last")
        except tk.TclError: return "break"
        if selection:
            top = self.winfo_toplevel()
            top.clipboard_clear()
            top.clipboard_append(selection)
        return "break"

    def _select_all_terminal(self, event=None):
        self.chat_display.tag_add("sel", "1.0", "end-1c")
        return "break"

    def _show_terminal_context_menu(self, event):
        menu = tk.Menu(self.winfo_toplevel(), tearoff=0)
        menu.add_command(label="Copy", command=self._copy_selection)
        menu.add_command(label="Select All", command=self._select_all_terminal)
        menu.add_separator()
        menu.add_command(label="Clear Terminal (keep memory)", command=self._clear_terminal_display)
        try: menu.tk_popup(event.x_root, event.y_root)
        finally: menu.grab_release()

    def inject_ollama_start_btn(self):
        self.chat_display.config(state=tk.NORMAL)
        btn_frame = tk.Frame(self.chat_display, bg=self.app.theme["bg_main"])
        btn = tk.Button(btn_frame, text="▶ Try to start Ollama", command=self.app._start_ollama, bg=self.app.theme["accent_blue"], fg="white", relief="flat", padx=10, pady=4)
        btn.pack(side=tk.LEFT)
        self.chat_display.window_create(tk.END, window=btn_frame)
        self.chat_display.insert(tk.END, "\n\n")
        self.chat_display.config(state=tk.DISABLED)
        self._check_scroll_btn()