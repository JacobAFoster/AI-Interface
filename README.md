# Local AI Dev & Business Hub

A fully local, privacy-focused AI assistant and automated coding environment built with Python and Tkinter. It integrates a powerful chat interface with calendar management, task tracking, phone notifications, and a robust **AI Batch Coder**, all powered by your local Ollama models.

**No data ever leaves your machine.**

## Features

### 🧠 Local Chat & Automation
- **Local Chat**: Connect to local LLMs via Ollama. Features multi-conversation history, streaming responses, and robust Markdown/code block rendering.
- **Auto-Coder (Batch Runner)**: Feed the AI a list of code changes. It clones your project folder, applies the edits, validates the syntax/diff, and writes the changes safely with automatic snapshots and rollbacks.
- **AI Batch Generation**: When you ask the AI for a list of code fixes, it automatically generates a ````batch` JSON block. Click "Send to Batch Tab" to instantly populate your coding queue.

### 📅 Productivity & Organization
- **AI Calendar**: Manage events effortlessly. Ask the AI to schedule a meeting, and it will generate a structured JSON block. Just click "Add to Calendar" to save it.
- **AI Tasks**: Keep track of your to-dos. Ask the AI to add a task with a due date and priority, and it will generate a clickable task card.
- **Custom Dashboard**: A fully modular home screen. Toggle on/off widgets like Context Window Gauge, Today's Agenda, Recent Batch Files, System Network Ping, Local Weather, and Windows Media Controls.
- **Phone Reminders**: Get push notifications on your phone for upcoming events and tasks using the free ntfy service.

### 🎨 Customization
- **Theme Engine**: Complete control over the look and feel. Change the hex codes for backgrounds, text, borders, and accents directly from the Settings menu with a built-in color picker. 
- **Custom Personas**: Switch between different AI personalities (e.g., Seasoned Tech Lead, Startup Founder, or create your own in `config/personalities.txt`).

---

## Project Structure

This project is modularized for maintainability, separating business logic from the user interface.

- `app.py` - Main entry point, root window, and streaming orchestrator.
- `config/` - Easy-to-edit configuration files
    - `models.txt` - List of Ollama models to load
    - `personalities.txt` - Custom AI personas (supports multi-line prompts)
    - `prompts.txt` - Saved prompt snippets for the chat library
- `core/` - Business logic & data layer
    - `db.py` - SQLite database operations
    - `parsing.py` - JSON & timestamp parsing logic
    - `context.py` - System prompt builders
    - `reminders.py` - Background ntfy notification service
    - `batch_editor.py` - Core logic for the AI code-generation runner
- `ui/` - Tkinter User Interface
    - `dashboard.py` - Customizable home screen widgets
    - `chat.py` - Chat tab & Markdown rendering
    - `calendar.py` - Calendar tab
    - `tasks.py` - Tasks tab
    - `batch_tab.py` - The AI Batch Runner control panel
    - `sidebar.py` - Sidebar navigation, model selection & settings
    - `theme.py` - Central color definitions and Tkinter styling
- `data/` - (Auto-generated) Local database, history, and batch snapshots

---

## How the Batch Coder Works

The Batch Runner is designed to be safe and autonomous:
1. **Clone**: It copies your source folder to a temporary clone directory (ignoring `.git`, `__pycache__`, etc.).
2. **Apply**: It feeds the file and the task to the LLM, asking for the *full* updated file.
3. **Validate**: It checks the proposed code for syntax errors, ensures the diff isn't too large, and optionally runs `ruff`.
4. **Auto-Progress**: If validation fails, it automatically retries with a different approach (e.g., "minimal edit" or "strict formatting").
5. **Review**: You review the color-coded diff and click **Approve**. It writes the file and saves a snapshot so you can always roll back. 
