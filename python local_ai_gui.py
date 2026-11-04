import tkinter as tk
from tkinter import scrolledtext, ttk
import subprocess
import threading
import os
import time
import re

class LocalAIApp:
    def __init__(self, root):
        self.root = root
        self.root.title("Local AI Dev & Business Hub (Obsidian Linked)")
        self.root.geometry("1200x800")
        
        # Obsidian Vault Path Configuration (Defaults to standard Windows Documents path)
        self.obsidian_vault_path = os.path.join(os.path.expanduser("~"), "Documents", "Obsidian Vault")

        # Configure modern style
        style = ttk.Style()
        style.theme_use("clam")

        # Load personalities from external file
        self.personalities = {}
        self.load_personalities_file()

        self.current_model = tk.StringVar(value="")
        self.current_persona = tk.StringVar(value="")
        
        self.show_debug_var = tk.BooleanVar(value=False)
        
        # Timer tracking variables
        self.is_thinking = False
        self.start_time = 0
        self.timer_after_id = None

        # --- Main Layout (Grid Split) ---
        self.root.columnconfigure(1, weight=1)
        self.root.rowconfigure(0, weight=1)

        # Left Sidebar Frame
        self.sidebar = ttk.Frame(self.root, padding=12)
        self.sidebar.grid(row=0, column=0, sticky="nsew")
        self.sidebar.rowconfigure(1, weight=1)
        self.sidebar.rowconfigure(4, weight=1)
        self.sidebar.columnconfigure(0, weight=1)

        # Right Terminal Frame
        self.terminal_frame = ttk.Frame(self.root, padding=12)
        self.terminal_frame.grid(row=0, column=1, sticky="nsew")
        self.terminal_frame.rowconfigure(2, weight=1)
        self.terminal_frame.columnconfigure(0, weight=1)

        # --- Sidebar Components ---
        # 1. Models Section
        ttk.Label(self.sidebar, text="🤖 Select Model", font=("Segoe UI", 10, "bold")).grid(row=0, column=0, sticky="w", pady=(0, 3))
        
        self.model_listbox = tk.Listbox(self.sidebar, height=8, selectmode=tk.SINGLE, exportselection=False, font=("Consolas", 9))
        self.model_listbox.grid(row=1, column=0, sticky="nsew", pady=(0, 6))
        self.model_listbox.bind("<<ListboxSelect>>", self.on_model_select)

        # Refresh Models Button (Reloads & checks installs)
        ttk.Button(self.sidebar, text="🔄 Sync & Reload Models", command=self.load_and_sync_models).grid(row=2, column=0, sticky="ew", pady=(0, 10))

        # 2. Personalities Section
        ttk.Label(self.sidebar, text="💼 Select Professional Role", font=("Segoe UI", 10, "bold")).grid(row=3, column=0, sticky="w", pady=(0, 3))
        
        self.persona_listbox = tk.Listbox(self.sidebar, height=6, selectmode=tk.SINGLE, exportselection=False, font=("Segoe UI", 9))
        self.persona_listbox.grid(row=4, column=0, sticky="nsew", pady=(0, 6))
        self.persona_listbox.bind("<<ListboxSelect>>", self.on_persona_select)
        
        for p in self.personalities.keys():
            self.persona_listbox.insert(tk.END, p)
        if self.personalities:
            self.persona_listbox.selection_set(0)
            self.current_persona.set(list(self.personalities.keys())[0])

        # Reload Personalities Button
        ttk.Button(self.sidebar, text="🔄 Reload Personalities", command=self.reload_personalities).grid(row=5, column=0, sticky="ew", pady=(0, 10))

        # 3. Debug Toggle Checkbutton
        ttk.Checkbutton(self.sidebar, text="Show Backend/Sampler Logs", variable=self.show_debug_var).grid(row=6, column=0, sticky="w", pady=(2, 5))

        # --- Terminal Components ---
        ttk.Label(self.terminal_frame, text="💻 Dev & Business Terminal", font=("Segoe UI", 11, "bold")).grid(row=0, column=0, sticky="w", pady=(0, 2))

        # Status & Timer Indicator Label
        self.status_label = ttk.Label(self.terminal_frame, text="Status: Idle", font=("Segoe UI", 9, "italic"), foreground="gray")
        self.status_label.grid(row=1, column=0, sticky="w", pady=(0, 4))

        # Chat ScrolledText & Soft Dark Theme Tag Configurations
        self.chat_display = scrolledtext.ScrolledText(self.terminal_frame, wrap=tk.WORD, bg="#1e1e1e", fg="#d4d4d4", font=("Consolas", 10), insertbackground="white")
        self.chat_display.grid(row=2, column=0, sticky="nsew", pady=(0, 10))
        
        # Configure contrasting background tints and fonts for message blocks
        self.chat_display.tag_config("user_header", foreground="#4ec9b0", font=("Segoe UI", 10, "bold"), lmargin1=5, lmargin2=5, spacing1=6)
        self.chat_display.tag_config("user_body", foreground="#d4d4d4", background="#252526", font=("Consolas", 10), lmargin1=10, lmargin2=10, rmargin=10, spacing3=6)
        
        self.chat_display.tag_config("ai_header", foreground="#c586c0", font=("Segoe UI", 10, "bold"), lmargin1=5, lmargin2=5, spacing1=6)
        self.chat_display.tag_config("ai_body", foreground="#d4d4d4", background="#1e1e1e", font=("Consolas", 10), lmargin1=10, lmargin2=10, rmargin=10, spacing3=6)
        
        self.chat_display.tag_config("debug_body", foreground="#858585", background="#1e1e1e", font=("Consolas", 9, "italic"), lmargin1=10, lmargin2=10, rmargin=10, spacing3=2)
        
        self.chat_display.config(state=tk.DISABLED)

        input_subframe = ttk.Frame(self.terminal_frame)
        input_subframe.grid(row=3, column=0, sticky="ew")
        input_subframe.columnconfigure(0, weight=1)

        # Multi-line code input text field with Ghost Text Placeholder
        self.user_input_field = tk.Text(input_subframe, height=4, font=("Consolas", 10), wrap=tk.WORD)
        self.user_input_field.grid(row=0, column=0, sticky="nsew", padx=(0, 8))
        
        self.placeholder_text = "Type your prompt here... (Press Enter to send, Shift+Enter for newline)"
        self.has_placeholder = True
        self.user_input_field.insert("1.0", self.placeholder_text)
        self.user_input_field.config(fg="gray")

        # Event bindings for placeholder & keys
        self.user_input_field.bind("<FocusIn>", self.on_focus_in)
        self.user_input_field.bind("<FocusOut>", self.on_focus_out)
        self.user_input_field.bind("<Return>", self.handle_return_key)
        self.user_input_field.bind("<Shift-Return>", lambda event: None)

        self.send_button = ttk.Button(input_subframe, text="Execute 🚀", command=self.send_query)
        self.send_button.grid(row=0, column=1, sticky="ne")

        self.load_and_sync_models()

    def load_personalities_file(self):
        """Loads professional roles and system prompts from a local personalities.txt file."""
        personalities_file = "personalities.txt"
        default_personalities = {
            "Seasoned Tech Lead": "You are an elite veteran software engineer and technical lead. Focus on architectural best practices, clean code, performance, scalability, security, and robust error handling. Provide precise, production-grade code and logic.",
            "Startup Founder": "You are a pragmatic, growth-driven startup founder and serial entrepreneur. Focus on MVPs, monetization strategies, rapid execution, market validation, and turning ideas into profitable products. Cut the fluff.",
            "Product Manager": "You are a sharp product manager and business strategist. Help break down feature lists, define user flows, prioritize scope, and figure out how to maximize value and user retention.",
            "Code Reviewer & QA": "You are a meticulous code reviewer and QA specialist. Examine code for hidden bugs, edge cases, performance bottlenecks, memory leaks, and security risks. Be thorough, precise, and critical.",
            "Junior Dev Visionary": "You are an ambitious, high-energy developer with big feature ideas. You love brainstorming creative ways to build cool apps quickly and explore experimental tech stacks.",
            "Systems Architect": "You are a high-level systems architect. Focus on database design, backend infrastructure, API design, scalability bottlenecks, and choosing the right technology stack for heavy workloads."
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
        """Reloads professional roles from personalities.txt and updates the listbox."""
        self.personalities = {}
        self.load_personalities_file()
        
        self.persona_listbox.delete(0, tk.END)
        for p in self.personalities.keys():
            self.persona_listbox.insert(tk.END, p)
            
        if self.personalities:
            self.persona_listbox.selection_set(0)
            self.current_persona.set(list(self.personalities.keys())[0])
            
        self.status_label.config(text="Status: Personalities reloaded successfully", foreground="green")
        self.root.after(2500, lambda: self.status_label.config(text="Status: Idle", foreground="gray"))

    def on_focus_in(self, event):
        """Clears the placeholder ghost text when the user clicks into the input box."""
        if self.has_placeholder:
            self.user_input_field.delete("1.0", tk.END)
            self.user_input_field.config(fg="#d4d4d4")
            self.has_placeholder = False

    def on_focus_out(self, event):
        """Restores the placeholder ghost text if the box is left empty."""
        if not self.user_input_field.get("1.0", tk.END).strip():
            self.user_input_field.insert("1.0", self.placeholder_text)
            self.user_input_field.config(fg="gray")
            self.has_placeholder = True

    def load_and_sync_models(self):
        """Reads models.txt, checks installed Ollama models, and pulls missing ones in a background thread."""
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
                        def update_status(m=model):
                            self.status_label.config(text=f"📥 Installing missing model: {m}...", foreground="blue")
                        self.root.after(0, update_status)
                        
                        try:
                            subprocess.run(["ollama", "pull", model], check=True, capture_output=True)
                        except Exception as e:
                            print(f"Failed to auto-install {model}: {e}")

                def finalize_ui():
                    self.update_model_ui(desired_models)
                    self.status_label.config(text="Status: Idle", foreground="gray")
                
                self.root.after(0, finalize_ui)

            except Exception as e:
                self.root.after(0, lambda: self.update_model_ui([f"Error reading {models_file}"]))
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
            if not val.startswith("Error") and not val.startswith("Loading"):
                self.current_model.set(val)

    def on_persona_select(self, event):
        selection = self.persona_listbox.curselection()
        if selection:
            self.current_persona.set(self.persona_listbox.get(selection[0]))

    def append_to_terminal(self, sender, text):
        self.chat_display.config(state=tk.NORMAL)
        self.chat_display.insert(tk.END, f"\n[{sender}]\n", "user_header")
        self.chat_display.insert(tk.END, f"{text}\n\n", "user_body")
        self.chat_display.see(tk.END)
        self.chat_display.config(state=tk.DISABLED)

    def append_ai_header(self, sender):
        """Inserts the color-coded AI header when the first token arrives."""
        self.chat_display.config(state=tk.NORMAL)
        self.chat_display.insert(tk.END, f"\n[{sender}]\n", "ai_header")
        self.chat_display.see(tk.END)
        self.chat_display.config(state=tk.DISABLED)

    def stream_chunk_to_terminal(self, text_chunk):
        """Appends chunks in real-time, filtering spinners/ANSI unless debug toggle is checked."""
        ansi_escape = re.compile(r'\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])')
        clean_chunk = ansi_escape.sub('', text_chunk)
        
        is_spinner_or_meta = any(char in clean_chunk for char in ['⠙', '⠸', '⠴', '⠧', '⠹', '⠼', '⠦', '⠇', '⠏']) or "llama" in clean_chunk.lower() or "slot" in clean_chunk.lower()
        
        if clean_chunk:
            self.chat_display.config(state=tk.NORMAL)
            y_view = self.chat_display.yview()
            is_at_bottom = y_view[1] >= 0.95

            if is_spinner_or_meta:
                if self.show_debug_var.get():
                    self.chat_display.insert(tk.END, f"[DEBUG] {clean_chunk.strip()}\n", "debug_body")
            else:
                self.chat_display.insert(tk.END, clean_chunk, "ai_body")
            
            if is_at_bottom:
                self.chat_display.see(tk.END)
                
            self.chat_display.config(state=tk.DISABLED)

    def start_timer(self):
        """Begins counting up elapsed thinking time."""
        self.is_thinking = True
        self.start_time = time.time()
        self.update_timer()

    def update_timer(self):
        """Recursively updates the status label every second while thinking."""
        if self.is_thinking:
            elapsed = int(time.time() - self.start_time)
            self.status_label.config(text=f"🧠 Thinking... ({elapsed}s elapsed)", foreground="orange")
            self.timer_after_id = self.root.after(1000, self.update_timer)

    def stop_timer(self):
        """Stops the timer loop and resets status text."""
        self.is_thinking = False
        if self.timer_after_id:
            self.root.after_cancel(self.timer_after_id)
            self.timer_after_id = None
        self.status_label.config(text="Status: Idle", foreground="gray")

    def log_to_obsidian(self, current_model, current_persona, system_prompt, user_query, ai_response):
        """Automatically organizes clean logs into Project/Topic folders in Obsidian."""
        try:
            ansi_escape = re.compile(r'\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])')
            cleaned_response = ansi_escape.sub('', ai_response)

            projects_dir = os.path.join(self.obsidian_vault_path, "Projects", "Local AI Hub")
            topics_dir = os.path.join(self.obsidian_vault_path, "Topics", current_persona.replace(" ", "_"))
            
            os.makedirs(projects_dir, exist_ok=True)
            os.makedirs(topics_dir, exist_ok=True)
            
            safe_model_name = "".join(c for c in current_model if c.isalnum() or c in (' ', '_', '-', '.')).strip()
            
            if "User Query:" in cleaned_response:
                parts = cleaned_response.split("User Query:")
                cleaned_response = parts[-1].strip()

            dashboard_file = os.path.join(self.obsidian_vault_path, "🏠 AI Dashboard.md")
            topic_file = os.path.join(topics_dir, f"{current_persona}.md")
            
            dashboard_content = "# 🏠 AI Control Center\n\n## Quick Links\n- [[🏠 AI Dashboard|Dashboard]]\n\n## Active Categories\n"
            if os.path.exists(dashboard_file):
                with open(dashboard_file, "r", encoding="utf-8") as f:
                    dashboard_content = f.read()
            
            role_link = f"- [[Topics/{current_persona.replace(' ', '_')}/{current_persona}|{current_persona}]] ({current_model})\n"
            if role_link.strip() not in dashboard_content:
                dashboard_content += role_link
                with open(dashboard_file, "w", encoding="utf-8") as f:
                    f.write(dashboard_content)

            if not os.path.exists(topic_file):
                frontmatter = f"""---
tags:
  - ai-log
  - role/{current_persona.lower().replace(' ', '-')}
  - model/{safe_model_name}
type: topic-log
---
# Topic: {current_persona}

> **System Directive:** {system_prompt}
> **Primary Model:** `{current_model}`

---

## Interaction History
"""
                with open(topic_file, "w", encoding="utf-8") as f:
                    f.write(frontmatter)
                    
            log_entry = f"\n### Query Log\n**User:** {user_query}\n\n**Response:**\n{cleaned_response}\n\n---\n"
            with open(topic_file, "a", encoding="utf-8") as f:
                f.write(log_entry)
                
        except Exception as ex:
            print(f"Obsidian Logging Error: {ex}")

    def handle_return_key(self, event):
        """Allows Enter to submit, while letting Shift+Enter add a newline."""
        if event.state & 0x1:
            return None
        else:
            self.send_query()
            return "break"

    def send_query(self):
        if self.has_placeholder:
            return
            
        query = self.user_input_field.get("1.0", tk.END).strip()
        model = self.current_model.get()
        persona_key = self.current_persona.get()
        
        if not query or not model:
            return

        self.user_input_field.delete("1.0", tk.END)
        self.user_input_field.insert("1.0", self.placeholder_text)
        self.user_input_field.config(fg="gray")
        self.has_placeholder = True

        self.append_to_terminal("You", query)
        self.start_timer()
        
        system_prompt = self.personalities.get(persona_key, "")
        final_prompt = f"System Instruction: {system_prompt}\n\nUser Query: {query}"

        def run_inference():
            try:
                process = subprocess.Popen(
                    ["ollama", "run", model, final_prompt],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    encoding='utf-8'
                )
                
                first_chunk = True
                accumulated_response = []
                
                for line in iter(process.stdout.readline, ''):
                    if first_chunk:
                        self.root.after(0, self.stop_timer)
                        self.root.after(0, lambda: self.append_ai_header(f"AI ({model} | {persona_key})"))
                        first_chunk = False
                    
                    accumulated_response.append(line)
                    self.root.after(0, lambda chunk=line: self.stream_chunk_to_terminal(chunk))
                
                process.wait()
                self.root.after(0, lambda: self.stream_chunk_to_terminal("\n\n"))
                
                full_output = "".join(accumulated_response).strip()
                self.log_to_obsidian(model, persona_key, system_prompt, query, full_output)
                
            except Exception as e:
                self.root.after(0, self.stop_timer)
                self.root.after(0, lambda: self.append_to_terminal("System Error", str(e)))

        threading.Thread(target=run_inference, daemon=True).start()

if __name__ == "__main__":
    root = tk.Tk()
    app = LocalAIApp(root)
    root.mainloop()