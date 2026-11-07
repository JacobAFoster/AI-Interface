# Local AI Dev & Business Hub

A fully local, privacy-focused AI assistant built with Python and Tkinter. It integrates a powerful chat interface with calendar management, task tracking, and phone notifications, all powered by your local Ollama models.

No data ever leaves your machine.

## Features

- **Local Chat:** Connect to local LLMs via [Ollama](https://ollama.com/). Features multi-conversation history, streaming responses, and robust Markdown/code block rendering.
- **AI Calendar:** Manage events effortlessly. Ask the AI to schedule a meeting, and it will generate a structured JSON block. Just click "Add to Calendar" to save it.
- **AI Tasks:** Keep track of your to-dos. Ask the AI to add a task with a due date and priority, and it will generate a clickable task card.
- **Phone Reminders:** Get push notifications on your phone for upcoming events and tasks using the free [ntfy](https://ntfy.sh/) service.
- **Custom Personas:** Switch between different AI personalities (e.g., Seasoned Tech Lead, Startup Founder, or create your own).
- **100% Private:** Your chats, calendar, and tasks are stored locally in a lightweight SQLite database. 

## Project Structure

This project is modularized for maintainability, separating business logic from the user interface.


product/
├── app.py                 # Main entry point & orchestrator
├── config/                # Easy-to-edit configuration files
│   ├── models.txt         # List of Ollama models to load
│   └── personalities.txt  # Custom AI personas
├── core/                  # Business logic & data layer
│   ├── db.py              # SQLite database operations
│   ├── parsing.py         # JSON & timestamp parsing logic
│   ├── context.py         # System prompt builders
│   └── reminders.py       # Background ntfy notification service
├── ui/                    # Tkinter User Interface
│   ├── chat.py            # Chat tab & Markdown rendering
│   ├── calendar.py        # Calendar tab
│   ├── tasks.py           # Tasks tab
│   └── sidebar.py         # Sidebar navigation & settings
└── data/                  # (Auto-generated) Local database & history
