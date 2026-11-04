import tkinter as tk
from tkinter import scrolledtext, ttk
import subprocess
import threading
import os
import time
import json
import urllib.request
import urllib.error


class LocalAIApp:
    MAX_HISTORY_MESSAGES = 20
    OLLAMA_CHAT_URL = "http://localhost:11434/api/chat"

    def __init__(self, root):
        self.root = root
        self.root.title("Local AI Dev & Business Hub")
        self.root.geometry("1200x800")

        style = ttk.Style()
        style.theme_use("clam")

        self.personalities = {}
        self.load_personalities_file()

        self.current_model = tk.StringVar(value="")
        self.current_persona = tk.StringVar(value="")

        self.is_thinking = False
        self.is_streaming = False
        self.start_time = 0
        self.timer_after_id = None

        self.conversation_history = []

        # --- Main Layout ---
        self.root.columnconfigure(1, weight=1)
        self.root.rowconfigure(0, weight=1)

        self.sidebar = ttk.Frame(self.root, padding=12)
        self.sidebar.grid(row=0, column=0, sticky="nsew")
        self.sidebar.rowconfigure(1, weight=1)
        self.sidebar.rowconfigure(4, weight=1)
        self.sidebar.columnconfigure(0, weight=1)

        self.terminal_frame = ttk.Frame(self.root, padding=12)
        self.terminal_frame.grid(row=0, column=1, sticky="nsew")
        self.terminal_frame.rowconfigure(2, weight=1)
        self.terminal_frame.columnconfigure(0, weight=1)

        # --- Sidebar ---
        ttk.Label(self.sidebar, text="🤖 Select Model", font=("Segoe UI", 10, "bold")).grid(row=0, column=0, sticky="w", pady=(0, 3))

        self.model_listbox = tk.Listbox(self.sidebar, height=8, selectmode=tk.SINGLE, exportselection=False, font=("Consolas", 9))
        self.model_listbox.grid(row=1, column=0, sticky="nsew", pady=(0, 6))
        self.model_listbox.bind("<<ListboxSelect>>", self.on_model_select)

        ttk.Button(self.sidebar, text="🔄 Sync & Reload Models", command=self.load_and_sync_models).grid(row=2, column=0, sticky="ew", pady=(0, 10))

        ttk.Label(self.sidebar, text="💼 Select Professional Role", font=("Segoe UI", 10, "bold")).grid(row=3, column=0, sticky="w", pady=(0, 3))

        self.persona_listbox = tk.Listbox(self.sidebar, height=6, selectmode=tk.SINGLE, exportselection=False, font=("Segoe UI", 9))
        self.persona_listbox.grid(row=4, column=0, sticky="nsew", pady=(0, 6))
        self.persona_listbox.bind("<<ListboxSelect>>", self.on_persona_select)

        for p in self.personalities.keys():
            self.persona_listbox.insert(tk.END, p)
        if self.personalities:
            self.persona_listbox.selection_set(0)
            self.current_persona.set(list(self.personalities.keys())[0])

        ttk.Button(self.sidebar, text="🔄 Reload Personalities", command=self.reload_personalities).grid(row=5, column=0, sticky="ew", pady=(0, 10))

        ttk.Button(self.sidebar, text="🗑️ Clear Conversation", command=self.clear_conversation).grid(row=6, column=0, sticky="ew", pady=(10, 0))

        self.history_label = ttk.Label(self.sidebar, text="Memory: 0 messages", font=("Segoe UI", 9, "italic"), foreground="gray")
        self.history_label.grid(row=7, column=0, sticky="w", pady=(4, 0))

        # --- Terminal ---
        ttk.Label(self.terminal_frame, text="💻 Dev & Business Terminal", font=("Segoe UI", 11, "bold")).grid(row=0, column=0, sticky="w", pady=(0, 2))

        self.status_label = ttk.Label(self.terminal_frame, text="Status: Idle", font=("Segoe UI", 9, "italic"), foreground="gray")
        self.status_label.grid(row=1, column=0, sticky="w", pady=(0, 4))

        self.chat_display = scrolledtext.ScrolledText(self.terminal_frame, wrap=tk.WORD, bg="#1e1e1e", fg="#d4d4d4", font=("Consolas", 10), insertbackground="white")
        self.chat_display.grid(row=2, column=0, sticky="nsew", pady=(0, 10))

        self.chat_display.tag_config("user_header", foreground="#4ec9b0", font=("Segoe UI", 10, "bold"), lmargin1=5, lmargin2=5, spacing1=6)
        self.chat_display.tag_config("user_body", foreground="#d4d4d4", background="#252526", font=("Consolas", 10), lmargin1=10, lmargin2=10, rmargin=10, spacing3=6)
        self.chat_display.tag_config("ai_header", foreground="#c586c0", font=("Segoe UI", 10, "bold"), lmargin1=5, lmargin2=5, spacing1=6)
        self.chat_display.tag_config("ai_body", foreground="#d4d4d4", background="#1e1e1e", font=("Consolas", 10), lmargin1=10, lmargin2=10, rmargin=10, spacing3=6)
        self.chat_display.tag_config("error_body", foreground="#f48771", font=("Consolas", 10), lmargin1=10, lmargin2=10, spacing3=4)

        self.chat_display.config(state=tk.DISABLED)

        # --- Copy / paste support on the read-only terminal ---
        self.chat_display.bind("<Control-c>", self._copy_selection)
        self.chat_display.bind("<Control-C>", self._copy_selection)
        self.chat_display.bind("<Control-a>", self._select_all_terminal)
        self.chat_display.bind("<Control-A>", self._select_all_terminal)
        self.chat_display.bind("<Button-3>", self._show_terminal_context_menu)
        # macOS users: right click is Button-2
        self.chat_display.bind("<Button-2>", self._show_terminal_context_menu)

        input_subframe = ttk.Frame(self.terminal_frame)
        input_subframe.grid(row=3, column=0, sticky="ew")
        input_subframe.columnconfigure(0, weight=1)

        self.user_input_field = tk.Text(input_subframe, height=4, font=("Consolas", 10), wrap=tk.WORD)
        self.user_input_field.grid(row=0, column=0, sticky="nsew", padx=(0, 8))

        self.placeholder_text = "Type your prompt here... (Enter to send, Shift+Enter for newline)"
        self.has_placeholder = True
        self.user_input_field.insert("1.0", self.placeholder_text)
        self.user_input_field.config(fg="gray")

        self.user_input_field.bind("<FocusIn>", self.on_focus_in)
        self.user_input_field.bind("<FocusOut>", self.on_focus_out)
        self.user_input_field.bind("<Return>", self.handle_return_key)
        self.user_input_field.bind("<Shift-Return>", lambda event: None)

        btn_frame = ttk.Frame(input_subframe)
        btn_frame.grid(row=0, column=1, sticky="ne")

        self.send_button = ttk.Button(btn_frame, text="Execute 🚀", command=self.send_query)
        self.send_button.pack(side=tk.TOP, fill="x", pady=(0, 4))

        self.research_button = ttk.Button(btn_frame, text="Deep Research 🔍", command=self.start_deep_research)
        self.research_button.pack(side=tk.TOP, fill="x")

        self.load_and_sync_models()

    # --- Copy / select-all / context menu on terminal ---
    def _copy_selection(self, event=None):
        try:
            selection = self.chat_display.get("sel.first", "sel.last")
        except tk.TclError:
            return "break"
        if selection:
            self.root.clipboard_clear()
            self.root.clipboard_append(selection)
        return "break"

    def _select_all_terminal(self, event=None):
        self.chat_display.tag_add("sel", "1.0", "end-1c")
        return "break"

    def _clear_terminal_display(self):
        self.chat_display.config(state=tk.NORMAL)
        self.chat_display.delete("1.0", tk.END)
        self.chat_display.config(state=tk.DISABLED)
        self.flash_status("Status: Terminal cleared (memory retained)", "green")

    def _show_terminal_context_menu(self, event):
        menu = tk.Menu(self.root, tearoff=0)
        menu.add_command(label="Copy", command=self._copy_selection)
        menu.add_command(label="Select All", command=self._select_all_terminal)
        menu.add_separator()
        menu.add_command(label="Clear Terminal (keep memory)", command=self._clear_terminal_display)
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    # --- Personalities ---
    def load_personalities_file(self):
        personalities_file = "personalities.txt"
        default_personalities = {
            "Seasoned Tech Lead": "You are an elite veteran software engineer and technical lead. Focus on architectural best practices, clean code, performance, scalability, security, and robust error handling.",
            "Startup Founder": "You are a pragmatic, growth-driven startup founder and serial entrepreneur. Focus on MVPs, monetization strategies, rapid execution, and market validation.",
            "Deep Research Analyst": "You are an elite research scientist and investigative analyst. Focus on uncovering comprehensive facts, synthesizing complex information from multiple angles, examining historical and technical context, maintaining strict objectivity, and structuring findings into clear, exhaustive reports with rigorous detail."
        }
        try:
            if not os.path.exists(personalities_file):
                with open(personalities_file, "w", encoding="utf-8") as f:
                    for name, prompt in default_personalities.items():
                        f.write(f"{name} = {prompt}\n")
            with open(personalities_file, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith("#") and "=" in line:
                        parts = line.split("=", 1)
                        self.personalities[parts[0].strip()] = parts[1].strip()
            if not self.personalities:
                self.personalities = default_personalities
        except Exception as e:
            print(f"Error loading personalities: {e}")
            self.personalities = default_personalities

    def reload_personalities(self):
        self.personalities = {}
        self.load_personalities_file()
        self.persona_listbox.delete(0, tk.END)
        for p in self.personalities.keys():
            self.persona_listbox.insert(tk.END, p)
        if self.personalities:
            self.persona_listbox.selection_set(0)
            self.current_persona.set(list(self.personalities.keys())[0])
        self.flash_status("Status: Personalities reloaded", "green")

    # --- Input placeholder ---
    def on_focus_in(self, event):
        if self.has_placeholder:
            self.user_input_field.delete("1.0", tk.END)
            self.user_input_field.config(fg="#d4d4d4")
            self.has_placeholder = False

    def on_focus_out(self, event):
        if not self.user_input_field.get("1.0", tk.END).strip():
            self.user_input_field.insert("1.0", self.placeholder_text)
            self.user_input_field.config(fg="gray")
            self.has_placeholder = True

    def handle_return_key(self, event):
        if event.state & 0x1:  # Shift held -> newline
            return None
        self.send_query()
        return "break"

    def _reset_input_field(self):
        self.user_input_field.delete("1.0", tk.END)
        self.user_input_field.insert("1.0", self.placeholder_text)
        self.user_input_field.config(fg="gray")
        self.has_placeholder = True

    # --- Model management ---
    def load_and_sync_models(self):
        self.model_listbox.delete(0, tk.END)
        self.model_listbox.insert(0, "Checking models...")
        self.status_label.config(text="Status: Syncing models...", foreground="orange")

        def background_sync():
            models_file = "models.txt"
            try:
                if not os.path.exists(models_file):
                    with open(models_file, "w", encoding="utf-8") as f:
                        f.write("llama3.2:1b\nqwen2.5:1.5b\n")
                with open(models_file, "r", encoding="utf-8") as f:
                    desired_models = [line.strip() for line in f if line.strip() and not line.startswith("#")]
                if not desired_models:
                    desired_models = ["llama3.2:1b"]

                installed_models = []
                try:
                    result = subprocess.run(["ollama", "list"], capture_output=True, text=True, check=True)
                    lines = result.stdout.strip().split("\n")[1:]
                    installed_models = [line.split()[0] for line in lines if line.strip()]
                except Exception:
                    pass

                for model in desired_models:
                    if model not in installed_models:
                        self.root.after(0, lambda m=model: self.status_label.config(text=f"📥 Installing missing model: {m}...", foreground="blue"))
                        try:
                            subprocess.run(["ollama", "pull", model], check=True, capture_output=True)
                        except Exception as e:
                            print(f"Failed to auto-install {model}: {e}")

                self.root.after(0, lambda: self.update_model_ui(desired_models))
                self.root.after(0, lambda: self.status_label.config(text="Status: Idle", foreground="gray"))
            except Exception as e:
                self.root.after(0, lambda: self.update_model_ui([f"Error: {e}"]))
                self.root.after(0, lambda: self.status_label.config(text="Status: Sync Error", foreground="red"))

        threading.Thread(target=background_sync, daemon=True).start()

    def update_model_ui(self, models):
        self.model_listbox.delete(0, tk.END)
        for m in models:
            self.model_listbox.insert(tk.END, m)
        if models and not models[0].startswith("Error"):
            self.model_listbox.selection_set(0)
            self.current_model.set(models[0])

    def on_model_select(self, event):
        selection = self.model_listbox.curselection()
        if selection:
            val = self.model_listbox.get(selection[0])
            if not val.startswith(("Error", "Loading", "Checking")):
                self.current_model.set(val)

    def on_persona_select(self, event):
        selection = self.persona_listbox.curselection()
        if selection:
            self.current_persona.set(self.persona_listbox.get(selection[0]))

    # --- Terminal helpers ---
    def append_to_terminal(self, sender, text):
        self.chat_display.config(state=tk.NORMAL)
        self.chat_display.insert(tk.END, f"\n[{sender}]\n", "user_header")
        self.chat_display.insert(tk.END, f"{text}\n\n", "user_body")
        self.chat_display.see(tk.END)
        self.chat_display.config(state=tk.DISABLED)

    def append_ai_header(self, sender):
        self.chat_display.config(state=tk.NORMAL)
        self.chat_display.insert(tk.END, f"\n[{sender}]\n", "ai_header")
        self.chat_display.see(tk.END)
        self.chat_display.config(state=tk.DISABLED)

    def append_error(self, message):
        self.chat_display.config(state=tk.NORMAL)
        self.chat_display.insert(tk.END, f"{message}\n", "error_body")
        self.chat_display.see(tk.END)
        self.chat_display.config(state=tk.DISABLED)

    def stream_chunk_to_terminal(self, text_chunk):
        if not text_chunk:
            return
        self.chat_display.config(state=tk.NORMAL)
        y_view = self.chat_display.yview()
        is_at_bottom = y_view[1] >= 0.95
        self.chat_display.insert(tk.END, text_chunk, "ai_body")
        if is_at_bottom:
            self.chat_display.see(tk.END)
        self.chat_display.config(state=tk.DISABLED)

    # --- Timer / status ---
    def start_timer(self):
        self.is_thinking = True
        self.start_time = time.time()
        self.update_timer()

    def update_timer(self):
        if self.is_thinking:
            elapsed = int(time.time() - self.start_time)
            self.status_label.config(text=f"🧠 Thinking... ({elapsed}s elapsed)", foreground="orange")
            self.timer_after_id = self.root.after(1000, self.update_timer)

    def stop_timer(self):
        self.is_thinking = False
        if self.timer_after_id:
            self.root.after_cancel(self.timer_after_id)
            self.timer_after_id = None
        self.status_label.config(text="Status: Idle", foreground="gray")

    def flash_status(self, message, color="green", duration=2500):
        self.status_label.config(text=message, foreground=color)
        self.root.after(duration, lambda: self.status_label.config(text="Status: Idle", foreground="gray"))

    # --- Conversation memory ---
    def _update_history_label(self):
        self.history_label.config(text=f"Memory: {len(self.conversation_history)} messages")

    def clear_conversation(self):
        self.conversation_history = []
        self._update_history_label()
        self.chat_display.config(state=tk.NORMAL)
        self.chat_display.delete("1.0", tk.END)
        self.chat_display.config(state=tk.DISABLED)
        self.flash_status("Status: Conversation cleared", "green")

    # --- Send / streaming ---
    def send_query(self):
        if self.has_placeholder:
            return
        query = self.user_input_field.get("1.0", tk.END).strip()
        if not query:
            return
        self._reset_input_field()
        self._send_message(query)

    def start_deep_research(self):
        if self.has_placeholder:
            return
        topic = self.user_input_field.get("1.0", tk.END).strip()
        if not topic:
            return
        self._reset_input_field()
        research_query = (
            f"Perform an in-depth, comprehensive research report on the following topic: '{topic}'.\n\n"
            "Provide a structured breakdown including:\n"
            "1. Executive Summary & Overview\n"
            "2. Key Concepts & Core Principles\n"
            "3. Practical Applications & Use Cases\n"
            "4. Potential Challenges & Future Outlook\n\n"
            "Write thoroughly and in detail."
        )
        self._send_message(research_query, display_text=topic, sender="Deep Research")

    def _send_message(self, query, display_text=None, sender="You"):
        if self.is_streaming:
            self.flash_status("Status: Wait for the current response to finish", "orange")
            return

        model = self.current_model.get()
        persona_key = self.current_persona.get()
        if not query or not model:
            return

        self.append_to_terminal(sender, display_text if display_text is not None else query)
        self.start_timer()

        system_prompt = self.personalities.get(persona_key, "")

        messages = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.extend(self.conversation_history[-self.MAX_HISTORY_MESSAGES:])
        messages.append({"role": "user", "content": query})

        self.is_streaming = True
        threading.Thread(
            target=self._run_chat_stream,
            args=(model, messages, persona_key, query),
            daemon=True
        ).start()

    def _run_chat_stream(self, model, messages, persona_key, user_message):
        payload = json.dumps({
            "model": model,
            "messages": messages,
            "stream": True
        }).encode("utf-8")

        req = urllib.request.Request(
            self.OLLAMA_CHAT_URL,
            data=payload,
            headers={"Content-Type": "application/json"}
        )

        accumulated = []
        header_shown = False

        try:
            with urllib.request.urlopen(req, timeout=600) as response:
                for raw_line in response:
                    if not raw_line.strip():
                        continue
                    try:
                        chunk = json.loads(raw_line.decode("utf-8"))
                    except json.JSONDecodeError:
                        continue

                    if not header_shown:
                        self.root.after(0, self.stop_timer)
                        self.root.after(0, lambda m=model, p=persona_key: self.append_ai_header(f"AI ({m} | {p})"))
                        header_shown = True

                    if "message" in chunk:
                        content = chunk["message"].get("content", "")
                        if content:
                            accumulated.append(content)
                            self.root.after(0, lambda c=content: self.stream_chunk_to_terminal(c))

                    if chunk.get("done", False):
                        break

            self.root.after(0, lambda: self.stream_chunk_to_terminal("\n\n"))
            full_response = "".join(accumulated).strip()

            if full_response:
                self.conversation_history.append({"role": "user", "content": user_message})
                self.conversation_history.append({"role": "assistant", "content": full_response})
                if len(self.conversation_history) > self.MAX_HISTORY_MESSAGES:
                    self.conversation_history = self.conversation_history[-self.MAX_HISTORY_MESSAGES:]
                self.root.after(0, self._update_history_label)

        except urllib.error.URLError as e:
            self.root.after(0, self.stop_timer)
            self.root.after(0, lambda msg=str(e): self.append_error(
                f"⚠️ Cannot connect to Ollama ({msg}). Is `ollama serve` running?"
            ))
        except Exception as e:
            self.root.after(0, self.stop_timer)
            self.root.after(0, lambda msg=str(e): self.append_error(f"⚠️ Error: {msg}"))
        finally:
            self.root.after(0, self._on_stream_finished)

    def _on_stream_finished(self):
        self.is_streaming = False
        self.stop_timer()


if __name__ == "__main__":
    root = tk.Tk()
    app = LocalAIApp(root)
    root.mainloop()