"""Persistence layer: paths, SQLite connection, and all CRUD helpers."""
import os
import sys
import json
import sqlite3
import shutil
from contextlib import contextmanager, closing
from datetime import datetime

NO_PERSONA = "(None — default assistant)"
MAX_MESSAGES_PER_CONVERSATION = 200
DEFAULT_REMINDER_MINUTES = 30

def _resolve_data_dir():
    if getattr(sys, "frozen", False):
        base = os.path.dirname(sys.executable)
    else:
        base = os.path.dirname(os.path.abspath(__file__))
        base = os.path.dirname(base)
    d = os.path.join(base, "data")
    os.makedirs(d, exist_ok=True)
    return d

_DATA_DIR = _resolve_data_dir()

CALENDAR_DB = os.environ.get("CALENDAR_DB", os.path.join(_DATA_DIR, "calendar.db"))
NOTIFIER_CONFIG = os.environ.get("NOTIFIER_CONFIG", os.path.join(_DATA_DIR, "notifier.json"))
APP_CONFIG = os.environ.get("APP_CONFIG", os.path.join(_DATA_DIR, "app_config.json"))
MODELS_CACHE = os.path.join(_DATA_DIR, "models_cache.json")
NTFY_URL = os.environ.get("NTFY_URL", "https://ntfy.sh/")

BASE_DIR = os.path.dirname(_DATA_DIR)
CONFIG_DIR = os.path.join(BASE_DIR, "config")
os.makedirs(CONFIG_DIR, exist_ok=True)

PERSONALITIES_FILE = os.path.join(CONFIG_DIR, "personalities.txt")
MODELS_FILE = os.path.join(CONFIG_DIR, "models.txt")
PROMPTS_FILE = os.path.join(CONFIG_DIR, "prompts.txt")
PARSE_LOG = os.path.join(_DATA_DIR, "parse_failures.log")
PARSE_LOG_MAX_BYTES = 256 * 1024

def _migrate_cwd_files():
    cwd = os.getcwd()
    if os.path.abspath(cwd) == os.path.abspath(_DATA_DIR):
        return

    data_files = ("calendar.db", "calendar.db-wal", "calendar.db-shm",
                  "notifier.json", "app_config.json", "parse_failures.log", "models_cache.json")
    config_files = ("personalities.txt", "models.txt", "prompts.txt")

    for name in data_files:
        old = os.path.join(cwd, name)
        new = os.path.join(_DATA_DIR, name)
        if os.path.exists(old) and not os.path.exists(new):
            try: shutil.move(old, new)
            except Exception as e: print(f"Migration of {name} failed: {e}")

    for name in config_files:
        old = os.path.join(cwd, name)
        new = os.path.join(CONFIG_DIR, name)
        if os.path.exists(old) and not os.path.exists(new):
            try: shutil.move(old, new)
            except Exception as e: print(f"Migration of {name} failed: {e}")

def _init_db():
    with closing(sqlite3.connect(CALENDAR_DB)) as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL,
                start_ts TEXT NOT NULL, end_ts TEXT NOT NULL, description TEXT DEFAULT '',
                reminder_minutes INTEGER, recurrence TEXT DEFAULT '', created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS conversations (
                id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT, conversation_id INTEGER NOT NULL,
                role TEXT NOT NULL, content TEXT NOT NULL, created_at TEXT NOT NULL,
                FOREIGN KEY(conversation_id) REFERENCES conversations(id) ON DELETE CASCADE
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL, description TEXT DEFAULT '',
                due_ts TEXT, priority TEXT DEFAULT 'normal', done INTEGER DEFAULT 0,
                reminder_minutes INTEGER, recurrence TEXT DEFAULT '', created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)
        try: conn.execute("ALTER TABLE events ADD COLUMN reminder_minutes INTEGER")
        except sqlite3.OperationalError: pass
        try: conn.execute("ALTER TABLE events ADD COLUMN recurrence TEXT DEFAULT ''")
        except sqlite3.OperationalError: pass
        try: conn.execute("ALTER TABLE tasks ADD COLUMN reminder_minutes INTEGER")
        except sqlite3.OperationalError: pass
        try: conn.execute("ALTER TABLE tasks ADD COLUMN recurrence TEXT DEFAULT ''")
        except sqlite3.OperationalError: pass
        conn.execute("CREATE INDEX IF NOT EXISTS idx_messages_conv ON messages(conversation_id)")
        conn.commit()

@contextmanager
def _db():
    conn = sqlite3.connect(CALENDAR_DB, timeout=5)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

def _now_str():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

def _default_app_config():
    return {
        "model": "", "persona": NO_PERSONA,
        "ollama_url": "http://localhost:11434/api/chat",
        "allow_auto_pull": False, "num_ctx": 16384,
        "max_history_messages": 20, "temperature": 0.2,
        "top_p": 0.9, "repeat_penalty": 1.05,
        "default_reminder_minutes": 30, "num_predict": -1,
        "top_k": 40, "seed": 0, "chat_font_size": 10,
        "theme": "dark"
    }

def _load_app_config():
    cfg = _default_app_config()
    try:
        if os.path.exists(APP_CONFIG):
            with open(APP_CONFIG, "r", encoding="utf-8") as f:
                loaded = json.load(f)
            for k in cfg:
                if k in loaded: cfg[k] = loaded[k]
    except Exception as e: print(f"App config load error: {e}")
    return cfg

def _save_app_config(cfg):
    try:
        with open(APP_CONFIG, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2)
    except Exception as e: print(f"App config save error: {e}")

def _search_messages(query):
    with _db() as conn:
        rows = conn.execute(
            "SELECT c.id, c.title, m.content FROM messages m "
            "JOIN conversations c ON m.conversation_id = c.id "
            "WHERE m.content LIKE ? ORDER BY m.id DESC LIMIT 100", (f"%{query}%",)
        ).fetchall()
    return [dict(r) for r in rows]

def _create_conversation(title):
    now = _now_str()
    with _db() as conn:
        cur = conn.execute("INSERT INTO conversations (title, created_at, updated_at) VALUES (?, ?, ?)", (title, now, now))
        return cur.lastrowid

def _get_conversations():
    with _db() as conn:
        rows = conn.execute("SELECT id, title, updated_at FROM conversations ORDER BY updated_at DESC").fetchall()
    return [dict(r) for r in rows]

def _get_messages(conversation_id):
    with _db() as conn:
        rows = conn.execute("SELECT role, content FROM messages WHERE conversation_id = ? ORDER BY id ASC", (conversation_id,)).fetchall()
    return [{"role": r["role"], "content": r["content"]} for r in rows]

def _add_message(conversation_id, role, content):
    if conversation_id is None: return None
    now = _now_str()
    msg_id = None
    trimmed = False
    with _db() as conn:
        cur = conn.execute("INSERT INTO messages (conversation_id, role, content, created_at) VALUES (?, ?, ?, ?)", (conversation_id, role, content, now))
        msg_id = cur.lastrowid
        conn.execute("UPDATE conversations SET updated_at = ? WHERE id = ?", (now, conversation_id))
        count = conn.execute("SELECT COUNT(*) FROM messages WHERE conversation_id = ?", (conversation_id,)).fetchone()[0]
        excess = count - MAX_MESSAGES_PER_CONVERSATION
        if excess > 0:
            if excess % 2: excess += 1
            conn.execute("DELETE FROM messages WHERE id IN (SELECT id FROM messages WHERE conversation_id = ? ORDER BY id ASC LIMIT ?)", (conversation_id, excess))
            trimmed = True
    if trimmed:
        try:
            _trim_orphans_in_db(conversation_id)
        except Exception as e:
            print(f"Orphan trim error: {e}")
    return msg_id

def _delete_message(msg_id):
    if msg_id is None: return
    try:
        with _db() as conn: conn.execute("DELETE FROM messages WHERE id = ?", (msg_id,))
    except Exception as e: print(f"Message delete error: {e}")

def _trim_orphans_in_db(conversation_id):
    with _db() as conn:
        while True:
            first = conn.execute("SELECT id, role FROM messages WHERE conversation_id = ? ORDER BY id ASC LIMIT 1", (conversation_id,)).fetchone()
            if first is None or first["role"] == "user": break
            conn.execute("DELETE FROM messages WHERE id = ?", (first["id"],))

def _delete_conversation(conversation_id):
    with _db() as conn:
        conn.execute("DELETE FROM messages WHERE conversation_id = ?", (conversation_id,))
        conn.execute("DELETE FROM conversations WHERE id = ?", (conversation_id,))

def _rename_conversation(conversation_id, new_title):
    with _db() as conn:
        conn.execute("UPDATE conversations SET title = ?, updated_at = ? WHERE id = ?", (new_title, _now_str(), conversation_id))

def _add_task(title, description='', due_ts=None, priority='normal', reminder_minutes=DEFAULT_REMINDER_MINUTES, recurrence=''):
    if priority not in ('low', 'normal', 'high'): priority = 'normal'
    if reminder_minutes is None: reminder_minutes = DEFAULT_REMINDER_MINUTES
    try: reminder_minutes = int(reminder_minutes)
    except (TypeError, ValueError): reminder_minutes = DEFAULT_REMINDER_MINUTES
    if reminder_minutes < 0: reminder_minutes = 0
    with _db() as conn:
        cur = conn.execute("INSERT INTO tasks (title, description, due_ts, priority, reminder_minutes, recurrence) VALUES (?, ?, ?, ?, ?, ?)", (title, description or '', due_ts, priority, reminder_minutes, recurrence or ''))
        return cur.lastrowid

def _get_tasks(include_done=False):
    order = "CASE priority WHEN 'high' THEN 0 WHEN 'normal' THEN 1 WHEN 'low' THEN 2 ELSE 3 END, due_ts IS NULL, due_ts ASC, id ASC"
    with _db() as conn:
        if include_done: rows = conn.execute(f"SELECT * FROM tasks ORDER BY done ASC, {order}").fetchall()
        else: rows = conn.execute(f"SELECT * FROM tasks WHERE done = 0 ORDER BY {order}").fetchall()
    return [dict(r) for r in rows]

def _toggle_task(task_id):
    with _db() as conn: conn.execute("UPDATE tasks SET done = 1 - done WHERE id = ?", (task_id,))

def _update_task(task_id, title, description, due_ts, priority, reminder_minutes=DEFAULT_REMINDER_MINUTES, recurrence=''):
    if priority not in ('low', 'normal', 'high'): priority = 'normal'
    if reminder_minutes is None: reminder_minutes = DEFAULT_REMINDER_MINUTES
    try: reminder_minutes = int(reminder_minutes)
    except (TypeError, ValueError): reminder_minutes = DEFAULT_REMINDER_MINUTES
    if reminder_minutes < 0: reminder_minutes = 0
    with _db() as conn:
        conn.execute("UPDATE tasks SET title=?, description=?, due_ts=?, priority=?, reminder_minutes=?, recurrence=? WHERE id=?", (title, description or '', due_ts, priority, reminder_minutes, recurrence or '', task_id))

def _delete_task(task_id):
    with _db() as conn: conn.execute("DELETE FROM tasks WHERE id=?", (task_id,))


# --- Initialise on import: migrate legacy files, then ensure schema exists ---
try:
    _migrate_cwd_files()
    _init_db()
except Exception as e:
    print(f"DB initialisation error: {e}")