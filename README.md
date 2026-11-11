<div align="center">

# Local AI Dev & Business Hub

**A private, offline-first desktop assistant powered by [Ollama](https://ollama.com).**
Chat with local models, manage your calendar and tasks, receive phone reminders, and batch-edit code with AI assistance from a single window, with no cloud API and no data leaving your machine.

![Python](https://img.shields.io/badge/python-3.9%2B-3776AB?logo=python&logoColor=white)
![UI](https://img.shields.io/badge/UI-Tkinter-0e639c)
![LLM](https://img.shields.io/badge/LLM-Ollama-black)
![Storage](https://img.shields.io/badge/storage-SQLite-003B57?logo=sqlite&logoColor=white)
![Dependencies](https://img.shields.io/badge/dependencies-none-4ec9b0)

</div>

---

## Features

### Chat
- **Streaming responses** from any local Ollama model, rendered with lightweight Markdown (bold, inline code, code blocks with a **Copy Code** button, clickable links).
- **Persistent conversations** stored in SQLite: search, rename, and delete from the sidebar.
- **Personas**: pick a professional role (Seasoned Tech Lead, Startup Founder, Deep Research Analyst, or your own) that sets the system prompt.
- **Prompt Library**: reusable prompts loaded from `config/prompts.txt`, selectable from a dropdown.
- **Retry / Stop** a response at any time; **Show Prompt** previews exactly what the model will receive.
- **Image attachments** for vision-capable models.
- **Context gauge** showing approximate token usage against your `num_ctx`.

### Calendar and Tasks
- Month-grid calendar with a day-detail panel.
- Tasks with due dates, priorities (low / normal / high) and completion tracking.
- **Recurrence**: events and tasks can repeat daily, weekly or monthly. Completing a recurring task rolls it to its next occurrence.
- **Per-item reminders**: choose how many minutes before an event starts or a task is due (0 = off).

### AI-Created Events and Tasks
The assistant sees your upcoming events and open tasks. Ask it to *"schedule a dentist appointment next Tuesday at 3pm"* or *"add a task to buy groceries"* and it proposes a card. Click **Add to Calendar** or **Add to Tasks** to confirm. Nothing is saved until you approve it.

### Phone Reminders
Reminders are pushed to your phone through [ntfy.sh](https://ntfy.sh). Enter a topic, enable reminders, and choose your default warning times. A background service checks every 30 seconds and never sends the same reminder twice.

### Batch Code Editor
Point the app at a source folder, queue up tasks, and let the model edit a **cloned copy** of your project, so your original files are never touched until you approve.

- **Task queue**: add tasks by hand, load/save them from a text file (`file ::: description`), reorder them, or send them straight from chat.
- **Chat to Batch**: ask *"audit my code and propose fixes"* and the AI returns a `batch` block. Click **Send to Batch Tab** to populate the queue.
- **Validation checks** on every proposed change:

  | Check | What it does |
  |---|---|
  | `syntax` | Python/JSON must parse |
  | `not_identical` | The model must actually change something |
  | `diff_bounded` | Rejects rewrites larger than your max diff % |
  | `ruff` | Optional lint pass (requires `ruff` on your PATH) |
  | `llm_review` | Optional second-opinion review by the model (90 s cap) |

- **Auto-progress** cycles through different approaches until validation passes, or **review manually** with a side-by-side diff and Approve / Reject (with feedback) / Skip.
- Pause, resume or stop a run at any time.

### Dashboard
A customizable home screen with toggleable widgets: Context Gauge, Today's Agenda, Quick Tasks, Network (Ollama) Status, Recent Batch Files, Weather, Clock, and Media Controls (Windows only).

### Theme
Dark VS Code-style theme by default. Override any color via `theme_colors` in `app_config.json`.

---

## Installation

### Requirements
- **Python 3.9+** with Tkinter (included with the standard Windows/macOS installers; on Debian/Ubuntu: `sudo apt install python3-tk`)
- **[Ollama](https://ollama.com/download)** installed and running
- No `pip install` needed, the app uses only the Python standard library

### 1. Get the code
```bash
git clone https://github.com/<your-username>/<your-repo>.git
cd <your-repo>
```
Or download the ZIP from GitHub and extract it.

### 2. Check the folder layout
```
app.py
core/   batch_editor.py  context.py  db.py  parsing.py  reminders.py
ui/     batch_tab.py  calendar.py  chat.py  dashboard.py  sidebar.py  tasks.py  theme.py
```
If your files are all in one flat folder, run **`organize.bat`** (Windows) once and it will sort them into place.

### 3. Pull a model
```bash
ollama pull llama3.2:1b
```
Any model works. Larger models give better results for code editing.

### 4. Run
```bash
python app.py
```
On first launch the app creates `data/` (database and settings) and `config/` (editable text files) next to `app.py`.

---

## Quick start

1. **Pick a model** in the sidebar (click **Sync & Reload Models** if the list is empty).
2. **Chat**: type a prompt and press `Enter` (`Shift+Enter` for a new line).
3. **Try scheduling**: *"Book a team sync tomorrow at 2pm for 30 minutes"*, then click **Add to Calendar**.
4. **Try batch mode**: ask the AI to audit a file, click **Send to Batch Tab**, set your source and clone folders, and click **Start**.

### Keyboard shortcuts
| Shortcut | Action |
|---|---|
| `Ctrl+N` | New conversation |
| `Ctrl+L` | Clear the terminal view (memory is kept) |
| `Ctrl+F` | Focus the conversation filter |
| `Esc` | Stop the current response |

---

## Configuration

### Settings dialog (sidebar)
`num_ctx`, max history messages, temperature, top_p, and default reminder minutes.

### `config/` text files
| File | Format |
|---|---|
| `personalities.txt` | `Name = system prompt` (one per line, `#` for comments) |
| `prompts.txt` | `Name = prompt text` |
| `models.txt` | One Ollama model name per line |

Click **Reload Personalities** / **Sync & Reload Models** after editing.

### `data/app_config.json`
Advanced options: `ollama_url`, `repeat_penalty`, `top_k`, `num_predict`, `seed`, `chat_font_size`, `allow_auto_pull`, and `theme_colors`, for example:
```json
{
  "theme_colors": { "accent_blue": "#7c3aed", "bg_main": "#111111" }
}
```

### Environment variables (optional)
`CALENDAR_DB`, `NOTIFIER_CONFIG`, `APP_CONFIG`, `NTFY_URL` override the default file locations and the ntfy server.

---

## Setting up phone reminders

1. Install the **ntfy** app on your phone ([iOS](https://apps.apple.com/app/ntfy/id1625396347) / [Android](https://play.google.com/store/apps/details?id=io.heckel.ntfy)).
2. Subscribe to a hard-to-guess topic name, e.g. `myhub-8f3k2x`. Topics are public, so don't use something obvious.
3. Enter the same topic in the sidebar under **Phone Reminders**, tick **Enable reminders**, and press **Test**.

---

## Project Structure

```
├── app.py              # Main window, streaming, conversation logic
├── core/
│   ├── db.py           # SQLite schema, CRUD, app config
│   ├── parsing.py      # Date/JSON/event/task/batch parsing (no Tk)
│   ├── context.py      # System-prompt builders (calendar snapshot, rules)
│   ├── reminders.py    # ntfy sender + background reminder service
│   └── batch_editor.py # Batch runner, validation, diffing
├── ui/
│   ├── sidebar.py      chat.py      dashboard.py
│   ├── calendar.py     tasks.py     batch_tab.py
│   └── theme.py
├── config/             # personalities.txt, prompts.txt, models.txt
└── data/               # calendar.db, app_config.json, notifier.json, logs
```

---

## Troubleshooting

| Problem | Fix |
|---|---|
| *"Cannot connect to Ollama"* | Run `ollama serve`, or click **Try to start Ollama** in chat. |
| Model list is empty | Make sure `ollama list` works in a terminal, then **Sync & Reload Models**. |
| AI says it scheduled something but nothing appears | Only a proposal card creates an item. Check `data/parse_failures.log` for malformed output; smaller models sometimes break the JSON format. |
| `ModuleNotFoundError: core` | Run `python app.py` from the project root, with `core/` and `ui/` beside it. |
| Weather widget blank | It fetches from `wttr.in` and needs an internet connection. |

---

## Privacy

Chats, events and tasks live in a local SQLite file. The only outbound connections are your local Ollama server, and optionally **ntfy** (reminder titles and times) and **wttr.in** (weather widget). Disable the Weather widget and leave reminders off for a fully offline setup.

---

<div align="center">

Python | Tkinter | Ollama | SQLite

</div>
