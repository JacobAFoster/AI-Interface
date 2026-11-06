import tkinter as tk
from tkinter import scrolledtext, ttk, messagebox
import subprocess
import threading
import os
import re
import time
import json
import sqlite3
import urllib.request
import urllib.error
from datetime import datetime, timedelta
from contextlib import contextmanager

# --- DB helpers -----------------------------------------------------------
CALENDAR_DB = os.environ.get("CALENDAR_DB", "calendar.db")
NOTIFIER_CONFIG = os.environ.get("NOTIFIER_CONFIG", "notifier.json")
NO_PERSONA = "(None — default assistant)"
MAX_MESSAGES_PER_CONVERSATION = 200


def _init_db():
    with sqlite3.connect(CALENDAR_DB) as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                start_ts TEXT NOT NULL,
                end_ts TEXT NOT NULL,
                description TEXT DEFAULT '',
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS conversations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                conversation_id INTEGER NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY(conversation_id) REFERENCES conversations(id) ON DELETE CASCADE
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_messages_conv ON messages(conversation_id)")
        conn.commit()


_init_db()


@contextmanager
def _db():
    conn = sqlite3.connect(CALENDAR_DB)
    conn.row_factory = sqlite3.Row
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


# --- Conversation persistence --------------------------------------------
def _create_conversation(title):
    now = _now_str()
    with _db() as conn:
        cur = conn.execute(
            "INSERT INTO conversations (title, created_at, updated_at) VALUES (?, ?, ?)",
            (title, now, now)
        )
        return cur.lastrowid


def _get_conversations():
    with _db() as conn:
        rows = conn.execute(
            "SELECT id, title, updated_at FROM conversations ORDER BY updated_at DESC"
        ).fetchall()
    return [dict(r) for r in rows]


def _get_messages(conversation_id):
    with _db() as conn:
        rows = conn.execute(
            "SELECT role, content FROM messages WHERE conversation_id = ? ORDER BY id ASC",
            (conversation_id,)
        ).fetchall()
    return [{"role": r["role"], "content": r["content"]} for r in rows]


def _add_message(conversation_id, role, content):
    if conversation_id is None:
        return
    now = _now_str()
    with _db() as conn:
        conn.execute(
            "INSERT INTO messages (conversation_id, role, content, created_at) VALUES (?, ?, ?, ?)",
            (conversation_id, role, content, now)
        )
        conn.execute(
            "UPDATE conversations SET updated_at = ? WHERE id = ?",
            (now, conversation_id)
        )
        count = conn.execute(
            "SELECT COUNT(*) FROM messages WHERE conversation_id = ?",
            (conversation_id,)
        ).fetchone()[0]
        if count > MAX_MESSAGES_PER_CONVERSATION:
            excess = count - MAX_MESSAGES_PER_CONVERSATION
            conn.execute("""
                DELETE FROM messages WHERE id IN (
                    SELECT id FROM messages WHERE conversation_id = ? ORDER BY id ASC LIMIT ?
                )
            """, (conversation_id, excess))


def _delete_conversation(conversation_id):
    with _db() as conn:
        conn.execute("DELETE FROM messages WHERE conversation_id = ?", (conversation_id,))
        conn.execute("DELETE FROM conversations WHERE id = ?", (conversation_id,))


# --- Timestamp & JSON helpers --------------------------------------------
def _normalize_ts(s):
    if not s:
        return None
    s = s.strip()
    for fmt in ("%Y-%m-%dT%H:%M", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M",
                "%Y-%m-%d", "%Y-%m-%dT%H:%M:%S.%f"):
        try:
            return datetime.strptime(s, fmt).strftime("%Y-%m-%dT%H:%M")
        except ValueError:
            continue
    return s


def _parse_ts_strict(s):
    if not s:
        return None
    s = str(s).strip()
    if s.endswith("Z"):
        s = s[:-1]
    for fmt in ("%Y-%m-%dT%H:%M", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%S.%f",
                "%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt).strftime("%Y-%m-%dT%H:%M")
        except ValueError:
            continue
    return None


def _parse_ts_relaxed(s):
    strict = _parse_ts_strict(s)
    if strict:
        return strict
    if not s:
        return None
    raw = str(s).strip()
    low = raw.lower()

    now = datetime.now()
    base = None
    if "today" in low:
        base = now
    elif "tomorrow" in low:
        base = now + timedelta(days=1)
    elif "next monday" in low:
        base = now + timedelta(days=((0 - now.weekday()) % 7) or 7)
    elif "monday" in low:
        base = now + timedelta(days=(0 - now.weekday()) % 7)
    elif "tuesday" in low:
        base = now + timedelta(days=(1 - now.weekday()) % 7)
    elif "wednesday" in low:
        base = now + timedelta(days=(2 - now.weekday()) % 7)
    elif "thursday" in low:
        base = now + timedelta(days=(3 - now.weekday()) % 7)
    elif "friday" in low:
        base = now + timedelta(days=(4 - now.weekday()) % 7)
    elif "saturday" in low:
        base = now + timedelta(days=(5 - now.weekday()) % 7)
    elif "sunday" in low:
        base = now + timedelta(days=(6 - now.weekday()) % 7)

    m = re.search(r'(\d{1,2})(?::(\d{2}))?\s*(am|pm)?', low)
    hour, minute = None, 0
    if m:
        hour = int(m.group(1))
        minute = int(m.group(2) or 0)
        ampm = (m.group(3) or "").lower()
        if ampm == "pm" and hour < 12:
            hour += 12
        elif ampm == "am" and hour == 12:
            hour = 0

    if base is None and hour is None:
        return None
    dt = (base or now).replace(hour=hour or 0, minute=minute, second=0, microsecond=0)
    return dt.strftime("%Y-%m-%dT%H:%M")


def _find_json_objects(text):
    out = []
    i, n = 0, len(text)
    while i < n:
        if text[i] != "{":
            i += 1
            continue
        depth, in_str, esc, j = 0, False, False, i
        while j < n:
            c = text[j]
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = not in_str
            elif not in_str:
                if c == "{":
                    depth += 1
                elif c == "}":
                    depth -= 1
                    if depth == 0:
                        out.append(text[i:j+1])
                        i = j
                        break
            j += 1
        i += 1
    return out


def _strip_json_comments(s):
    if not s:
        return s
    out = []
    i, n = 0, len(s)
    in_str = False
    esc = False
    while i < n:
        c = s[i]
        if in_str:
            out.append(c)
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
            i += 1
            continue
        if c == '"':
            in_str = True
            out.append(c)
            i += 1
            continue
        if c == "/" and i + 1 < n:
            nxt = s[i + 1]
            if nxt == "/":
                j = i + 2
                while j < n and s[j] != "\n":
                    j += 1
                i = j
                continue
            if nxt == "*":
                j = i + 2
                while j + 1 < n and not (s[j] == "*" and s[j + 1] == "/"):
                    j += 1
                i = (j + 2) if (j + 1 < n) else n
                continue
        out.append(c)
        i += 1
    return "".join(out)


def _normalize_json_text(s):
    if not s:
        return s
    trans = {
        "\u201c": '"', "\u201d": '"',
        "\u2018": "'", "\u2019": "'",
        "\u00ab": '"', "\u00bb": '"',
        "\uff02": '"', "\uff07": "'",
        "\u2013": "-", "\u2014": "-",
        "\u00a0": " ",
    }
    s = s.translate(str.maketrans(trans))
    s = re.sub(r'([{,]\s*)([A-Za-z_][A-Za-z0-9_]*)(\s*:)', r'\1"\2"\3', s)
    s = re.sub(r'(:\s*)(\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2})?)(\s*[,}])',
               r'\1"\2"\3', s)
    s = re.sub(r',(\s*[}\]])', r'\1', s)
    return s


# --- Notifier config + ntfy sender ---------------------------------------
def _default_notifier_config():
    return {"enabled": False, "ntfy_topic": "", "lead_minutes": [15, 60]}


def _load_notifier_config():
    cfg = _default_notifier_config()
    try:
        if os.path.exists(NOTIFIER_CONFIG):
            with open(NOTIFIER_CONFIG, "r", encoding="utf-8") as f:
                loaded = json.load(f)
            for k in cfg:
                if k in loaded:
                    cfg[k] = loaded[k]
    except Exception as e:
        print(f"Notifier config load error: {e}")
    return cfg


def _save_notifier_config(cfg):
    try:
        with open(NOTIFIER_CONFIG, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2)
    except Exception as e:
        print(f"Notifier config save error: {e}")


def _send_ntfy(topic, title, message, priority="default", tags="calendar"):
    """POST to ntfy.sh using the JSON endpoint (avoids latin-1 header encoding
    so emojis in the title work). Returns True on success."""
    if not topic:
        return False
    prio_map = {"min": 1, "low": 2, "default": 3, "high": 4, "urgent": 5}
    priority_val = prio_map.get(priority, 3) if isinstance(priority, str) else priority

    payload = json.dumps({
        "topic": topic,
        "title": title,
        "message": message,
        "priority": priority_val,
        "tags": [tags] if isinstance(tags, str) else (tags or []),
    }).encode("utf-8")

    req = urllib.request.Request(
        "https://ntfy.sh/",
        data=payload,
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return 200 <= resp.status < 300
    except Exception as e:
        print(f"ntfy send failed: {e}")
        return False


# --- Reminder background service ----------------------------------------
class ReminderService:
    """Polls the calendar every 30s and sends ntfy notifications for upcoming events."""

    def __init__(self, app):
        self.app = app
        self._stop = threading.Event()
        self._thread = None
        self._notified = set()
        self._lock = threading.Lock()

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()

    def reset_notified(self):
        with self._lock:
            self._notified.clear()

    def _loop(self):
        while not self._stop.is_set():
            try:
                self._check()
            except Exception as e:
                print(f"Reminder check error: {e}")
            self._stop.wait(30)

    def _check(self):
        cfg = self.app.notifier_config
        if not cfg.get("enabled") or not cfg.get("ntfy_topic"):
            return
        now = datetime.now()
        leads = cfg.get("lead_minutes") or [15]
        max_lead = max(leads)
        horizon = now + timedelta(minutes=max_lead)

        with _db() as conn:
            rows = conn.execute(
                "SELECT id, title, start_ts, end_ts, description FROM events "
                "WHERE start_ts > ? AND start_ts <= ? ORDER BY start_ts",
                (now.strftime("%Y-%m-%dT%H:%M"),
                 horizon.strftime("%Y-%m-%dT%H:%M"))
            ).fetchall()

        for row in rows:
            ev = dict(row)
            try:
                start = datetime.strptime(ev["start_ts"], "%Y-%m-%dT%H:%M")
            except ValueError:
                continue

            for lead in leads:
                key = (ev["id"], lead)
                with self._lock:
                    if key in self._notified:
                        continue

                trigger = start - timedelta(minutes=lead)
                if now < trigger:
                    continue

                title = f"⏰ {ev['title']}"
                body = f"Starts at {start.strftime('%H:%M')} — {lead} min from now"
                if ev.get("description"):
                    body += f"\n{ev['description']}"

                if _send_ntfy(cfg["ntfy_topic"], title, body):
                    with self._lock:
                        self._notified.add(key)
                    self.app.root.after(
                        0,
                        lambda t=ev["title"]: self.app.flash_status(
                            f"🔔 Reminder sent: {t}", "green", 3000)
                    )


# --- Calendar context (injected into every chat prompt) ------------------
def _calendar_context():
    now = datetime.now()
    horizon = now + timedelta(days=14)
    with _db() as conn:
        rows = conn.execute(
            "SELECT * FROM events WHERE end_ts >= ? AND start_ts <= ? ORDER BY start_ts LIMIT 50",
            (now.strftime("%Y-%m-%dT%H:%M"), horizon.strftime("%Y-%m-%dT%H:%M"))
        ).fetchall()
    if rows:
        lines = [f"- {r['start_ts']} -> {r['end_ts']}: {r['title']}" +
                 (f" ({r['description']})" if r['description'] else "") for r in rows]
    else:
        lines = ["(no upcoming events)"]

    week_end = (now + timedelta(days=(6 - now.weekday()))).replace(hour=23, minute=59)
    this_week = [r for r in rows if r["start_ts"] <= week_end.strftime("%Y-%m-%dT%H:%M")]
    week_lines = [f"  - {r['start_ts']}: {r['title']}" for r in this_week] or ["  (none)"]

    return (
        f"\n\n[Calendar context]\n"
        f"Current local time: {now.strftime('%Y-%m-%dT%H:%M')} "
        f"({now.strftime('%A, %B %d, %Y')})\n"
        f"Upcoming events (next 14 days):\n" + "\n".join(lines) + "\n\n"
        f"This calendar week (Mon–Sun, ending {week_end.strftime('%a %b %d')}):\n"
        + "\n".join(week_lines) + "\n\n"
        "When the user asks you to schedule, propose, or reschedule events, include a "
        "fenced code block tagged 'calendar' containing a single JSON object with keys "
        "title, start_ts, end_ts, description. Use LOCAL time in format YYYY-MM-DDTHH:MM. "
        "Example:\n"
        "```calendar\n"
        '{"title": "Team sync", "start_ts": "2025-01-15T14:00", '
        '"end_ts": "2025-01-15T14:30", "description": "Weekly sync"}\n'
        "```\n"
        "You may emit multiple calendar blocks if the user wants several events. "
        "Always confirm the details in prose too.\n\n"
        "CRITICAL: Saying 'I have added the event' or 'It is scheduled' does NOT "
        "create the event. The system ONLY creates events when it sees a ```calendar "
        "block in your reply. If the user asks you to schedule ANYTHING, you MUST "
        "include a ```calendar block. No block = no event.\n\n"
        "FORMAT: The fence tag MUST be exactly ```calendar. "
        "The block MUST contain STRICT, VALID JSON — double quotes around keys and "
        "string values, NO comments of any kind (no //, no /* */), NO trailing commas, "
        "NO markdown, NO extra text inside the block. Only these keys: "
        "title, start_ts, end_ts, description. "
        "start_ts and end_ts MUST be local time in YYYY-MM-DDTHH:MM format."
    )


# --- Calendar Tab ---------------------------------------------------------
class CalendarTab:
    def __init__(self, parent, app):
        self.parent = parent
        self.app = app
        self.current_month = datetime.now().replace(day=1)
        self.selected_date = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        self.events_cache = []
        self._build_ui()
        self.refresh()

    def _build_ui(self):
        toolbar = ttk.Frame(self.parent)
        toolbar.pack(fill="x", pady=(0, 8))
        ttk.Button(toolbar, text="◀", width=3, command=self._prev_month).pack(side=tk.LEFT)
        self.title_label = ttk.Label(toolbar, text="", font=("Segoe UI", 13, "bold"))
        self.title_label.pack(side=tk.LEFT, padx=8)
        ttk.Button(toolbar, text="▶", width=3, command=self._next_month).pack(side=tk.LEFT)
        ttk.Button(toolbar, text="Today", command=self._goto_today).pack(side=tk.LEFT, padx=(8, 0))
        ttk.Button(toolbar, text="+ New Event", command=self._open_event_dialog).pack(side=tk.LEFT, padx=(8, 0))
        ttk.Button(toolbar, text="🔄 Refresh", command=self._manual_refresh).pack(side=tk.LEFT, padx=(8, 0))

        main = ttk.Frame(self.parent)
        main.pack(fill="both", expand=True)
        self.grid_frame = tk.Frame(main, bg="#333")
        self.grid_frame.pack(side=tk.LEFT, fill="both", expand=True)
        for i in range(7):
            self.grid_frame.columnconfigure(i, weight=1, uniform="cal")
        for i in range(7):
            self.grid_frame.rowconfigure(i, weight=(0 if i == 0 else 1), uniform="calrow")

        side = tk.Frame(main, bg="#252526", width=290)
        side.pack(side=tk.RIGHT, fill="y", padx=(8, 0))
        side.pack_propagate(False)
        self.side_date_label = tk.Label(side, text="", bg="#252526", fg="#4ec9b0",
                                        font=("Segoe UI", 11, "bold"), anchor="w")
        self.side_date_label.pack(fill="x", padx=10, pady=(10, 6))
        canvas = tk.Canvas(side, bg="#252526", highlightthickness=0)
        scrollbar = ttk.Scrollbar(side, orient="vertical", command=canvas.yview)
        self.side_inner = tk.Frame(canvas, bg="#252526")
        self.side_inner.bind("<Configure>",
                             lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.create_window((0, 0), window=self.side_inner, anchor="nw")
        canvas.configure(yscrollcommand=scrollbar.set)
        canvas.pack(side=tk.LEFT, fill="both", expand=True, padx=(10, 0), pady=(0, 10))
        scrollbar.pack(side=tk.RIGHT, fill="y", pady=(0, 10), padx=(0, 6))

    def _prev_month(self):
        self.current_month = (self.current_month - timedelta(days=1)).replace(day=1)
        self.refresh()

    def _next_month(self):
        if self.current_month.month == 12:
            self.current_month = self.current_month.replace(year=self.current_month.year + 1, month=1)
        else:
            self.current_month = self.current_month.replace(month=self.current_month.month + 1)
        self.refresh()

    def _goto_today(self):
        now = datetime.now()
        self.current_month = now.replace(day=1)
        self.selected_date = now.replace(hour=0, minute=0, second=0, microsecond=0)
        self.refresh()

    def _manual_refresh(self):
        self.refresh()
        self.app.flash_status("Status: Calendar refreshed", "green")

    def _select_date(self, d):
        self.selected_date = d.replace(hour=0, minute=0, second=0, microsecond=0)
        self._render()

    def refresh(self):
        y, m = self.current_month.year, self.current_month.month
        first = datetime(y, m, 1)
        grid_start = first - timedelta(days=(first.weekday() + 1) % 7)
        grid_end = grid_start + timedelta(days=42)
        with _db() as conn:
            rows = conn.execute(
                "SELECT * FROM events WHERE start_ts < ? AND end_ts > ? ORDER BY start_ts",
                (grid_end.strftime("%Y-%m-%dT%H:%M"), grid_start.strftime("%Y-%m-%dT%H:%M"))
            ).fetchall()
            self.events_cache = [dict(r) for r in rows]
        self._render()

    def _events_for_day(self, d):
        day_start = d.replace(hour=0, minute=0, second=0, microsecond=0)
        day_end = day_start + timedelta(days=1)
        ds = day_start.strftime("%Y-%m-%dT%H:%M")
        de = day_end.strftime("%Y-%m-%dT%H:%M")
        return [e for e in self.events_cache if e["start_ts"] < de and e["end_ts"] > ds]

    def _render(self):
        self.title_label.config(text=self.current_month.strftime("%B %Y"))
        for w in self.grid_frame.winfo_children():
            w.destroy()
        for i, day_name in enumerate(["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"]):
            tk.Label(self.grid_frame, text=day_name, bg="#252526", fg="#888",
                     font=("Segoe UI", 8, "bold")).grid(row=0, column=i, sticky="nsew", padx=1, pady=1)

        y, m = self.current_month.year, self.current_month.month
        first = datetime(y, m, 1)
        grid_start = first - timedelta(days=(first.weekday() + 1) % 7)
        today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)

        for i in range(42):
            d = grid_start + timedelta(days=i)
            row, col = (i // 7) + 1, i % 7
            is_other = d.month != m
            is_today = d.date() == today.date()
            is_sel = d.date() == self.selected_date.date()

            bg = "#181818" if is_other else "#1e1e1e"
            border_color = "#0e639c" if is_sel else "#333"
            cell = tk.Frame(self.grid_frame, bg=bg,
                            highlightthickness=2 if is_sel else 1,
                            highlightbackground=border_color, highlightcolor=border_color)
            cell.grid(row=row, column=col, sticky="nsew", padx=1, pady=1)

            num_bg = "#0e639c" if is_today else bg
            num_fg = "#fff" if is_today else ("#666" if is_other else "#d4d4d4")
            tk.Label(cell, text=str(d.day), bg=num_bg, fg=num_fg,
                     font=("Segoe UI", 9, "bold"), anchor="w").pack(fill="x", padx=4, pady=(3, 2))

            evs = self._events_for_day(d)
            for ev in evs[:2]:
                tk.Label(cell, text=ev["title"][:14], bg="#0e639c", fg="white",
                         font=("Segoe UI", 7), anchor="w").pack(fill="x", padx=2)
            if len(evs) > 2:
                tk.Label(cell, text=f"+{len(evs) - 2} more", bg=bg, fg="#888",
                         font=("Segoe UI", 7), anchor="w").pack(fill="x", padx=4)

            for w in [cell] + list(cell.winfo_children()):
                w.bind("<Button-1>", lambda e, dt=d: self._select_date(dt))
        self._render_side()

    def _render_side(self):
        self.side_date_label.config(text=self.selected_date.strftime("%A, %B %d, %Y"))
        for w in self.side_inner.winfo_children():
            w.destroy()
        events = self._events_for_day(self.selected_date)
        if not events:
            tk.Label(self.side_inner, text="No events", bg="#252526", fg="#666",
                     font=("Segoe UI", 9, "italic")).pack(anchor="w", pady=8)
            return
        for ev in events:
            card = tk.Frame(self.side_inner, bg="#1e1e1e", highlightthickness=1, highlightbackground="#333")
            card.pack(fill="x", pady=4)
            try:
                s = datetime.strptime(ev["start_ts"], "%Y-%m-%dT%H:%M")
                en = datetime.strptime(ev["end_ts"], "%Y-%m-%dT%H:%M")
                tstr = f"{s.strftime('%H:%M')} – {en.strftime('%H:%M')}"
            except Exception:
                tstr = f"{ev['start_ts']} → {ev['end_ts']}"
            tk.Label(card, text=tstr, bg="#1e1e1e", fg="#888",
                     font=("Consolas", 8), anchor="w").pack(fill="x", padx=8, pady=(6, 0))
            tk.Label(card, text=ev["title"], bg="#1e1e1e", fg="#d4d4d4",
                     font=("Segoe UI", 9, "bold"), anchor="w",
                     wraplength=250, justify="left").pack(fill="x", padx=8)
            if ev.get("description"):
                tk.Label(card, text=ev["description"], bg="#1e1e1e", fg="#aaa",
                         font=("Segoe UI", 8), anchor="w",
                         wraplength=250, justify="left").pack(fill="x", padx=8, pady=(2, 0))
            btn_row = tk.Frame(card, bg="#1e1e1e")
            btn_row.pack(fill="x", padx=8, pady=(4, 6))
            ttk.Button(btn_row, text="Edit", width=6,
                       command=lambda e=ev: self._open_event_dialog(e)).pack(side=tk.LEFT)
            ttk.Button(btn_row, text="Delete", width=6,
                       command=lambda e=ev: self._delete_event(e)).pack(side=tk.LEFT, padx=(4, 0))

    def _delete_event(self, ev):
        if not messagebox.askyesno("Delete Event", f"Delete '{ev['title']}'?"):
            return
        try:
            with _db() as conn:
                conn.execute("DELETE FROM events WHERE id=?", (ev["id"],))
            self.refresh()
            self.app.flash_status("Status: Event deleted", "green")
        except Exception as e:
            self.app.flash_status(f"Error: {e}", "red")

    def _open_event_dialog(self, event=None):
        dlg = tk.Toplevel(self.parent)
        dlg.title("Edit Event" if event else "New Event")
        dlg.transient(self.app.root)
        dlg.grab_set()
        dlg.resizable(False, False)
        pad = {"padx": 10, "pady": 4}

        row1 = ttk.Frame(dlg); row1.pack(fill="x", **pad)
        ttk.Label(row1, text="Title:", width=10).pack(side=tk.LEFT)
        title_var = tk.StringVar(value=event["title"] if event else "")
        ttk.Entry(row1, textvariable=title_var, width=42).pack(side=tk.LEFT)

        row2 = ttk.Frame(dlg); row2.pack(fill="x", **pad)
        ttk.Label(row2, text="Start:", width=10).pack(side=tk.LEFT)
        sv = (datetime.strptime(event["start_ts"], "%Y-%m-%dT%H:%M").strftime("%Y-%m-%d %H:%M")
              if event else self.selected_date.replace(hour=9, minute=0).strftime("%Y-%m-%d %H:%M"))
        start_var = tk.StringVar(value=sv)
        ttk.Entry(row2, textvariable=start_var, width=20).pack(side=tk.LEFT)
        ttk.Label(row2, text="  (YYYY-MM-DD HH:MM)", foreground="gray").pack(side=tk.LEFT)

        row3 = ttk.Frame(dlg); row3.pack(fill="x", **pad)
        ttk.Label(row3, text="End:", width=10).pack(side=tk.LEFT)
        ev_str = (datetime.strptime(event["end_ts"], "%Y-%m-%dT%H:%M").strftime("%Y-%m-%d %H:%M")
                  if event else self.selected_date.replace(hour=10, minute=0).strftime("%Y-%m-%d %H:%M"))
        end_var = tk.StringVar(value=ev_str)
        ttk.Entry(row3, textvariable=end_var, width=20).pack(side=tk.LEFT)

        row4 = ttk.Frame(dlg); row4.pack(fill="x", **pad)
        ttk.Label(row4, text="Description:").pack(anchor="w")
        desc_text = tk.Text(dlg, height=3, width=55, wrap=tk.WORD)
        desc_text.pack(fill="x", padx=10, pady=(0, 6))
        if event:
            desc_text.insert("1.0", event.get("description", ""))

        def _save():
            try:
                t = title_var.get().strip()
                s_raw = start_var.get().strip()
                e_raw = end_var.get().strip()
                if not t:
                    raise ValueError("Title is required")
                if not re.match(r"^\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}", s_raw):
                    raise ValueError("Start must be YYYY-MM-DD HH:MM")
                if not re.match(r"^\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}", e_raw):
                    raise ValueError("End must be YYYY-MM-DD HH:MM")
                s = _normalize_ts(s_raw)
                en = _normalize_ts(e_raw)
                d = desc_text.get("1.0", "end-1c").strip()
                with _db() as conn:
                    if event:
                        conn.execute(
                            "UPDATE events SET title=?, start_ts=?, end_ts=?, description=? WHERE id=?",
                            (t, s, en, d, event["id"]))
                    else:
                        conn.execute(
                            "INSERT INTO events (title, start_ts, end_ts, description) VALUES (?, ?, ?, ?)",
                            (t, s, en, d))
                dlg.destroy()
                self.refresh()
                self.app.flash_status("Status: Event saved", "green")
            except Exception as ex:
                messagebox.showerror("Error", str(ex), parent=dlg)

        btn_row = ttk.Frame(dlg); btn_row.pack(fill="x", padx=10, pady=(4, 10))
        ttk.Button(btn_row, text="Cancel", command=dlg.destroy).pack(side=tk.RIGHT)
        ttk.Button(btn_row, text="Save", command=_save).pack(side=tk.RIGHT, padx=(0, 6))


# --- Main App -------------------------------------------------------------
class LocalAIApp:
    MAX_HISTORY_MESSAGES = 20
    OLLAMA_CHAT_URL = "http://localhost:11434/api/chat"

    def __init__(self, root):
        self.root = root
        self.root.title("Local AI Dev & Business Hub")
        self.root.geometry("1280x840")

        style = ttk.Style()
        style.theme_use("clam")

        self.personalities = {}
        self.load_personalities_file()

        self.notifier_config = _load_notifier_config()

        self.current_model = tk.StringVar(value="")
        self.current_persona = tk.StringVar(value=NO_PERSONA)
        self.is_thinking = False
        self.is_streaming = False
        self.start_time = 0
        self.timer_after_id = None
        self.conversation_history = []

        self.current_conversation_id = None
        self.conv_id_map = {}

        self.root.columnconfigure(1, weight=1)
        self.root.rowconfigure(0, weight=1)

        self.sidebar = ttk.Frame(self.root, padding=12)
        self.sidebar.grid(row=0, column=0, sticky="nsew")
        self.sidebar.rowconfigure(1, weight=2)
        self.sidebar.rowconfigure(4, weight=1)
        self.sidebar.rowconfigure(7, weight=1)
        self.sidebar.columnconfigure(0, weight=1)

        self.notebook = ttk.Notebook(self.root)
        self.notebook.grid(row=0, column=1, sticky="nsew")
        self.terminal_frame = ttk.Frame(self.notebook, padding=12)
        self.notebook.add(self.terminal_frame, text="💬 Chat")
        self.terminal_frame.rowconfigure(2, weight=1)
        self.terminal_frame.columnconfigure(0, weight=1)
        self.calendar_frame = ttk.Frame(self.notebook, padding=12)
        self.notebook.add(self.calendar_frame, text="📅 Calendar")

        # --- Sidebar: Conversations ---
        ttk.Label(self.sidebar, text="💬 Conversations",
                  font=("Segoe UI", 10, "bold")).grid(row=0, column=0, sticky="w", pady=(0, 3))
        self.conversation_listbox = tk.Listbox(self.sidebar, height=8, selectmode=tk.SINGLE,
                                               exportselection=False, font=("Segoe UI", 9))
        self.conversation_listbox.grid(row=1, column=0, sticky="nsew", pady=(0, 4))
        self.conversation_listbox.bind("<<ListboxSelect>>", self._on_conversation_selected)

        conv_btn_frame = ttk.Frame(self.sidebar)
        conv_btn_frame.grid(row=2, column=0, sticky="ew", pady=(0, 10))
        conv_btn_frame.columnconfigure(0, weight=1)
        conv_btn_frame.columnconfigure(1, weight=1)
        ttk.Button(conv_btn_frame, text="+ New Chat",
                   command=self._new_conversation).grid(row=0, column=0, sticky="ew", padx=(0, 3))
        ttk.Button(conv_btn_frame, text="🗑 Delete",
                   command=self._delete_current_conversation).grid(row=0, column=1, sticky="ew", padx=(3, 0))

        # --- Sidebar: Models ---
        ttk.Label(self.sidebar, text="🤖 Select Model",
                  font=("Segoe UI", 10, "bold")).grid(row=3, column=0, sticky="w", pady=(0, 3))
        self.model_listbox = tk.Listbox(self.sidebar, height=5, selectmode=tk.SINGLE,
                                        exportselection=False, font=("Consolas", 9))
        self.model_listbox.grid(row=4, column=0, sticky="nsew", pady=(0, 6))
        self.model_listbox.bind("<<ListboxSelect>>", self.on_model_select)
        ttk.Button(self.sidebar, text="🔄 Sync & Reload Models",
                   command=self.load_and_sync_models).grid(row=5, column=0, sticky="ew", pady=(0, 8))

        # --- Sidebar: Personas ---
        ttk.Label(self.sidebar, text="💼 Select Professional Role",
                  font=("Segoe UI", 10, "bold")).grid(row=6, column=0, sticky="w", pady=(0, 3))
        self.persona_listbox = tk.Listbox(self.sidebar, height=4, selectmode=tk.SINGLE,
                                          exportselection=False, font=("Segoe UI", 9))
        self.persona_listbox.grid(row=7, column=0, sticky="nsew", pady=(0, 6))
        self.persona_listbox.bind("<<ListboxSelect>>", self.on_persona_select)
        self.persona_listbox.insert(tk.END, NO_PERSONA)
        for p in self.personalities.keys():
            self.persona_listbox.insert(tk.END, p)
        self.persona_listbox.selection_set(0)
        ttk.Button(self.sidebar, text="🔄 Reload Personalities",
                   command=self.reload_personalities).grid(row=8, column=0, sticky="ew", pady=(0, 8))

        # --- Sidebar: Phone Reminders ---
        ttk.Label(self.sidebar, text="🔔 Phone Reminders",
                  font=("Segoe UI", 10, "bold")).grid(row=9, column=0, sticky="w", pady=(0, 3))

        rem_frame = ttk.Frame(self.sidebar)
        rem_frame.grid(row=10, column=0, sticky="ew", pady=(0, 8))
        rem_frame.columnconfigure(1, weight=1)

        ttk.Label(rem_frame, text="ntfy topic:").grid(row=0, column=0, sticky="w")
        self.ntfy_topic_var = tk.StringVar(value=self.notifier_config.get("ntfy_topic", ""))
        ttk.Entry(rem_frame, textvariable=self.ntfy_topic_var).grid(row=0, column=1, sticky="ew", padx=(6, 0))
        self.ntfy_topic_var.trace_add("write", lambda *_: self._on_notifier_config_changed())

        self.reminder_enabled_var = tk.BooleanVar(value=bool(self.notifier_config.get("enabled")))
        ttk.Checkbutton(rem_frame, text="Enable reminders",
                        variable=self.reminder_enabled_var,
                        command=self._on_notifier_config_changed).grid(row=1, column=0, columnspan=2,
                                                                       sticky="w", pady=(4, 0))

        btn_rem = ttk.Frame(rem_frame)
        btn_rem.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(4, 0))
        ttk.Button(btn_rem, text="Test", command=self._test_notification).pack(side=tk.LEFT)
        ttk.Button(btn_rem, text="Help", command=self._show_ntfy_help).pack(side=tk.LEFT, padx=(4, 0))

        self.history_label = ttk.Label(self.sidebar, text="Memory: 0 messages",
                                       font=("Segoe UI", 9, "italic"), foreground="gray")
        self.history_label.grid(row=11, column=0, sticky="w", pady=(4, 0))

        # --- Terminal ---
        ttk.Label(self.terminal_frame, text="💻 Dev & Business Terminal",
                  font=("Segoe UI", 11, "bold")).grid(row=0, column=0, sticky="w", pady=(0, 2))
        self.status_label = ttk.Label(self.terminal_frame, text="Status: Idle",
                                      font=("Segoe UI", 9, "italic"), foreground="gray")
        self.status_label.grid(row=1, column=0, sticky="w", pady=(0, 4))
        self.chat_display = scrolledtext.ScrolledText(
            self.terminal_frame, wrap=tk.WORD, bg="#1e1e1e", fg="#d4d4d4",
            font=("Consolas", 10), insertbackground="white")
        self.chat_display.grid(row=2, column=0, sticky="nsew", pady=(0, 10))
        self.chat_display.tag_config("user_header", foreground="#4ec9b0",
                                     font=("Segoe UI", 10, "bold"), lmargin1=5, lmargin2=5, spacing1=6)
        self.chat_display.tag_config("user_body", foreground="#d4d4d4", background="#252526",
                                     font=("Consolas", 10), lmargin1=10, lmargin2=10, rmargin=10, spacing3=6)
        self.chat_display.tag_config("ai_header", foreground="#c586c0",
                                     font=("Segoe UI", 10, "bold"), lmargin1=5, lmargin2=5, spacing1=6)
        self.chat_display.tag_config("ai_body", foreground="#d4d4d4", background="#1e1e1e",
                                     font=("Consolas", 10), lmargin1=10, lmargin2=10, rmargin=10, spacing3=6)
        self.chat_display.tag_config("error_body", foreground="#f48771",
                                     font=("Consolas", 10), lmargin1=10, lmargin2=10, spacing3=4)
        self.chat_display.config(state=tk.DISABLED)
        self.chat_display.bind("<Control-c>", self._copy_selection)
        self.chat_display.bind("<Control-C>", self._copy_selection)
        self.chat_display.bind("<Control-a>", self._select_all_terminal)
        self.chat_display.bind("<Control-A>", self._select_all_terminal)
        self.chat_display.bind("<Button-3>", self._show_terminal_context_menu)
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

        self.calendar_tab = CalendarTab(self.calendar_frame, self)
        self.notebook.bind("<<NotebookTabChanged>>", self._on_tab_changed)

        # --- Reminder service ---
        self.reminder_service = ReminderService(self)
        self.reminder_service.start()

        # --- Startup: load most recent conversation ---
        self._refresh_conversation_listbox()
        if self.conversation_listbox.size() > 0:
            self.conversation_listbox.selection_set(0)
            cid = self.conv_id_map.get(0)
            if cid is not None:
                self._load_conversation(cid)

        self.load_and_sync_models()

        if self.notifier_config.get("enabled") and self.notifier_config.get("ntfy_topic"):
            self.root.after(1500, lambda: self.flash_status(
                f"🔔 Reminders active on topic '{self.notifier_config['ntfy_topic']}'",
                "green", 4000))

    # --- Notifier config handlers ---
    def _on_notifier_config_changed(self):
        self.notifier_config["ntfy_topic"] = self.ntfy_topic_var.get().strip()
        self.notifier_config["enabled"] = bool(self.reminder_enabled_var.get())
        _save_notifier_config(self.notifier_config)
        self.reminder_service.reset_notified()

    def _test_notification(self):
        topic = self.ntfy_topic_var.get().strip()
        if not topic:
            messagebox.showwarning("ntfy", "Enter a topic name first.")
            return
        ok = _send_ntfy(topic, "🔔 Test reminder", "If you see this on your phone, reminders work.")
        if ok:
            self.flash_status("🔔 Test notification sent — check your phone", "green", 4000)
        else:
            self.flash_status("⚠️ Test failed — check topic name and network", "#c33", 5000)

    def _show_ntfy_help(self):
        messagebox.showinfo(
            "Set up phone reminders with ntfy",
            "1. Install the free ntfy app on your phone (Android: Play Store, iOS: App Store).\n\n"
            "2. In the app, tap + and subscribe to a topic name — use something long and unique, "
            "e.g. 'alex-calendar-7f3a91c4'. The topic name acts as a password, so don't use 'calendar'.\n\n"
            "3. Type the same topic name in the box here and tick 'Enable reminders'.\n\n"
            "4. Click Test. You should get a push notification on your phone within a couple of seconds.\n\n"
            "The app checks every 30 seconds and sends reminders 15 minutes and 60 minutes before each event."
        )

    # --- Conversation management ---
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

    def _refresh_conversation_listbox(self):
        prev = self.current_conversation_id
        self.conversation_listbox.delete(0, tk.END)
        self.conv_id_map = {}
        for i, c in enumerate(_get_conversations()):
            self.conversation_listbox.insert(tk.END,
                                             self._format_conv_label(c["title"], c["updated_at"]))
            self.conv_id_map[i] = c["id"]
        if prev is not None:
            for i in range(self.conversation_listbox.size()):
                if self.conv_id_map.get(i) == prev:
                    self.conversation_listbox.selection_clear(0, tk.END)
                    self.conversation_listbox.selection_set(i)
                    self.conversation_listbox.see(i)
                    break

    def _on_conversation_selected(self, event=None):
        if self.is_streaming:
            self.flash_status("Status: Wait for current response to finish", "orange")
            if self.current_conversation_id is not None:
                for i in range(self.conversation_listbox.size()):
                    if self.conv_id_map.get(i) == self.current_conversation_id:
                        self.conversation_listbox.selection_clear(0, tk.END)
                        self.conversation_listbox.selection_set(i)
                        break
            return
        sel = self.conversation_listbox.curselection()
        if not sel:
            return
        cid = self.conv_id_map.get(sel[0])
        if cid is None or cid == self.current_conversation_id:
            return
        self._load_conversation(cid)

    def _load_conversation(self, cid):
        self.current_conversation_id = cid
        messages = _get_messages(cid)
        self.conversation_history = messages[-self.MAX_HISTORY_MESSAGES:]
        self.chat_display.config(state=tk.NORMAL)
        self.chat_display.delete("1.0", tk.END)
        self.chat_display.config(state=tk.DISABLED)
        for m in messages:
            self._render_message_to_terminal(m["role"], m["content"])
        self._update_history_label()
        self.flash_status(f"Status: Loaded conversation ({len(messages)} messages)", "green")

    def _new_conversation(self):
        if self.is_streaming:
            self.flash_status("Status: Wait for current response to finish", "orange")
            return
        self.current_conversation_id = None
        self.conversation_history = []
        self.chat_display.config(state=tk.NORMAL)
        self.chat_display.delete("1.0", tk.END)
        self.chat_display.config(state=tk.DISABLED)
        self.conversation_listbox.selection_clear(0, tk.END)
        self._update_history_label()
        self.flash_status("Status: New conversation started", "green")

    def _delete_current_conversation(self):
        if self.is_streaming:
            self.flash_status("Status: Wait for current response to finish", "orange")
            return
        if self.current_conversation_id is None:
            self.flash_status("Status: No conversation to delete", "orange")
            return
        if not messagebox.askyesno("Delete Conversation",
                                   "Delete this conversation? This cannot be undone."):
            return
        _delete_conversation(self.current_conversation_id)
        self.current_conversation_id = None
        self.conversation_history = []
        self.chat_display.config(state=tk.NORMAL)
        self.chat_display.delete("1.0", tk.END)
        self.chat_display.config(state=tk.DISABLED)
        self._refresh_conversation_listbox()
        if self.conversation_listbox.size() > 0:
            self.conversation_listbox.selection_set(0)
            cid = self.conv_id_map.get(0)
            if cid is not None:
                self._load_conversation(cid)
        else:
            self._update_history_label()
        self.flash_status("Status: Conversation deleted", "green")

    def _render_message_to_terminal(self, role, content):
        if role == "user":
            self.append_to_terminal("You", content)
        elif role == "assistant":
            cleaned = self._summarize_calendar_blocks(content)
            self.append_ai_header("AI")
            self.chat_display.config(state=tk.NORMAL)
            self.chat_display.insert(tk.END, cleaned + "\n\n", "ai_body")
            self.chat_display.see(tk.END)
            self.chat_display.config(state=tk.DISABLED)

    def _summarize_calendar_blocks(self, content):
        def replacer(match):
            block = _normalize_json_text(match.group(1).strip())
            block = _strip_json_comments(block)
            if block.lower().startswith("json"):
                block = block[4:].lstrip()
            s = block.find("{")
            e = block.rfind("}")
            if s >= 0 and e > s:
                try:
                    ev = json.loads(block[s:e+1])
                    return f"\n📅 [Event proposed: {ev.get('title','?')} at {ev.get('start_ts','?')}]\n"
                except json.JSONDecodeError:
                    pass
            return "\n📅 [Event proposed]\n"
        return re.sub(r"```\s*calendar\b[^\n]*\n?(.*?)```", replacer, content,
                      flags=re.DOTALL | re.IGNORECASE).strip()

    # --- Tab change / clipboard / context menu ---
    def _on_tab_changed(self, event=None):
        try:
            if self.notebook.index(self.notebook.select()) == 1 and hasattr(self, "calendar_tab"):
                self.calendar_tab.refresh()
        except Exception:
            pass

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
        pf = "personalities.txt"
        defaults = {
            "Seasoned Tech Lead": "You are an elite veteran software engineer and technical lead. Focus on architectural best practices, clean code, performance, scalability, security, and robust error handling.",
            "Startup Founder": "You are a pragmatic, growth-driven startup founder and serial entrepreneur. Focus on MVPs, monetization strategies, rapid execution, and market validation.",
            "Deep Research Analyst": "You are an elite research scientist and investigative analyst. Focus on uncovering comprehensive facts, synthesizing complex information from multiple angles, examining historical and technical context, maintaining strict objectivity, and structuring findings into clear, exhaustive reports with rigorous detail."
        }
        try:
            if not os.path.exists(pf):
                with open(pf, "w", encoding="utf-8") as f:
                    for n, p in defaults.items():
                        f.write(f"{n} = {p}\n")
            with open(pf, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith("#") and "=" in line:
                        k, v = line.split("=", 1)
                        self.personalities[k.strip()] = v.strip()
            if not self.personalities:
                self.personalities = defaults
        except Exception as e:
            print(f"Error loading personalities: {e}")
            self.personalities = defaults

    def reload_personalities(self):
        self.personalities = {}
        self.load_personalities_file()
        self.persona_listbox.delete(0, tk.END)
        self.persona_listbox.insert(tk.END, NO_PERSONA)
        for p in self.personalities.keys():
            self.persona_listbox.insert(tk.END, p)
        self.persona_listbox.selection_set(0)
        self.current_persona.set(NO_PERSONA)
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
        if event.state & 0x1:
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
                    desired = [l.strip() for l in f if l.strip() and not l.startswith("#")]
                if not desired:
                    desired = ["llama3.2:1b"]
                installed = []
                try:
                    r = subprocess.run(["ollama", "list"], capture_output=True, text=True, check=True)
                    installed = [line.split()[0] for line in r.stdout.strip().split("\n")[1:] if line.strip()]
                except Exception:
                    pass
                for m in desired:
                    if m not in installed:
                        self.root.after(0, lambda mm=m: self.status_label.config(
                            text=f"📥 Installing missing model: {mm}...", foreground="blue"))
                        try:
                            subprocess.run(["ollama", "pull", m], check=True, capture_output=True)
                        except Exception as e:
                            print(f"Failed to auto-install {m}: {e}")
                self.root.after(0, lambda: self.update_model_ui(desired))
                self.root.after(0, lambda: self.status_label.config(text="Status: Idle", foreground="gray"))
            except Exception as e:
                self.root.after(0, lambda err=e: self.update_model_ui([f"Error: {err}"]))
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
        sel = self.model_listbox.curselection()
        if sel:
            v = self.model_listbox.get(sel[0])
            if not v.startswith(("Error", "Loading", "Checking")):
                self.current_model.set(v)

    def on_persona_select(self, event):
        sel = self.persona_listbox.curselection()
        if sel:
            self.current_persona.set(self.persona_listbox.get(sel[0]))

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
        is_bottom = y_view[1] >= 0.95
        self.chat_display.insert(tk.END, text_chunk, "ai_body")
        if is_bottom:
            self.chat_display.see(tk.END)
        self.chat_display.config(state=tk.DISABLED)

    # --- Timer ---
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

    def _update_history_label(self):
        self.history_label.config(text=f"Memory: {len(self.conversation_history)} messages")

    # --- Send / stream ---
    def send_query(self):
        if self.has_placeholder:
            return
        q = self.user_input_field.get("1.0", tk.END).strip()
        if not q:
            return
        self._reset_input_field()
        self._send_message(q)

    def start_deep_research(self):
        if self.has_placeholder:
            return
        topic = self.user_input_field.get("1.0", tk.END).strip()
        if not topic:
            return
        self._reset_input_field()
        research_q = (
            f"Perform an in-depth, comprehensive research report on the following topic: '{topic}'.\n\n"
            "Provide a structured breakdown including:\n"
            "1. Executive Summary & Overview\n2. Key Concepts & Core Principles\n"
            "3. Practical Applications & Use Cases\n4. Potential Challenges & Future Outlook\n\n"
            "Write thoroughly and in detail."
        )
        self._send_message(research_q, display_text=topic, sender="Deep Research")

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
            self._refresh_conversation_listbox()

        try:
            _add_message(self.current_conversation_id, "user",
                         display_text if display_text is not None else query)
            self._refresh_conversation_listbox()
        except Exception as e:
            print(f"DB save user msg error: {e}")

        self.append_to_terminal(sender, display_text if display_text is not None else query)
        self.start_timer()

        if persona_key == NO_PERSONA:
            system_prompt = ""
        else:
            system_prompt = self.personalities.get(persona_key, "")
        system_prompt += _calendar_context()

        messages = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.extend(self.conversation_history[-self.MAX_HISTORY_MESSAGES:])
        messages.append({"role": "user", "content": query})

        self.is_streaming = True
        threading.Thread(target=self._run_chat_stream,
                         args=(model, messages, persona_key, query), daemon=True).start()

    def _run_chat_stream(self, model, messages, persona_key, user_message):
        payload = json.dumps({
            "model": model,
            "messages": messages,
            "stream": True,
            "options": {
                "temperature": 0.2,
                "top_p": 0.9,
                "repeat_penalty": 1.05,
                "num_ctx": 4096,
            }
        }).encode("utf-8")
        req = urllib.request.Request(self.OLLAMA_CHAT_URL, data=payload,
                                     headers={"Content-Type": "application/json"})
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
                        persona_label = "no persona" if persona_key == NO_PERSONA else persona_key
                        self.root.after(0, lambda lbl=f"AI ({model} | {persona_label})":
                                        self.append_ai_header(lbl))
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
                try:
                    _add_message(self.current_conversation_id, "assistant", full_response)
                except Exception as e:
                    print(f"DB save assistant msg error: {e}")
                self.root.after(0, self._update_history_label)
                self.root.after(0, self._refresh_conversation_listbox)
        except urllib.error.URLError as e:
            self.root.after(0, self.stop_timer)
            self.root.after(0, lambda msg=str(e): self.append_error(
                f"⚠️ Cannot connect to Ollama ({msg}). Is `ollama serve` running?"))
        except Exception as e:
            self.root.after(0, self.stop_timer)
            self.root.after(0, lambda msg=str(e): self.append_error(f"⚠️ Error: {msg}"))
        finally:
            self.root.after(0, self._on_stream_finished)

    def _on_stream_finished(self):
        self.is_streaming = False
        self.stop_timer()
        proposed = self._offer_calendar_suggestions()
        if proposed:
            n = len(proposed)
            self.flash_status(
                f"📅 {n} event{'s' if n > 1 else ''} proposed — click 'Add to Calendar' below",
                "orange", duration=8000)
        else:
            if self.conversation_history and self.conversation_history[-1].get("role") == "assistant":
                last = self.conversation_history[-1].get("content", "")
                looks_like_event = (
                    '"title"' in last or "'title'" in last or
                    '"start"' in last or '"start_ts"' in last or
                    "calendar" in last.lower()
                )
                if looks_like_event:
                    self.flash_status(
                        "⚠️ Model tried to schedule but format didn't parse "
                        "(check parse_failures.log)",
                        "#c33", duration=6000)
                else:
                    self.flash_status(
                        "⚠️ No calendar proposal in AI reply "
                        "(model didn't attempt to schedule anything)",
                        "#c33", duration=4000)

    # --- Calendar event extraction (lenient) ---
    def _offer_calendar_suggestions(self):
        if not self.conversation_history:
            return []
        last = self.conversation_history[-1]
        if last.get("role") != "assistant":
            return []
        events = self._extract_calendar_events(last.get("content", ""))
        if not events:
            return []
        self._render_calendar_suggestions(events)
        return events

    def _extract_calendar_events(self, text):
        events, seen = [], set()
        text = _normalize_json_text(text)

        def _try(candidate):
            ev = self._try_parse_event_json(candidate)
            if ev is None:
                return
            key = (ev["title"], ev["start_ts"], ev["end_ts"])
            if key in seen:
                return
            seen.add(key)
            events.append(ev)

        for m in re.finditer(r"```\s*calendar\b[^\n]*\n?(.*?)```",
                             text, re.DOTALL | re.IGNORECASE):
            block = m.group(1).strip()
            block = _strip_json_comments(block)
            if block.lower().startswith("json"):
                block = block[4:].lstrip()
            for obj in _find_json_objects(block):
                _try(obj)

        for m in re.finditer(r"```[^\n]*\n(.*?)```", text, re.DOTALL):
            block = m.group(1).strip()
            block = _strip_json_comments(block)
            for obj in _find_json_objects(block):
                _try(obj)

        for obj in _find_json_objects(_strip_json_comments(text)):
            _try(obj)

        if not events and any(k in text for k in ('"title"', "'title'", '"start', 'calendar')):
            try:
                with open("parse_failures.log", "a", encoding="utf-8") as f:
                    f.write("=" * 60 + "\n")
                    f.write(f"TIMESTAMP: {datetime.now().isoformat()}\n")
                    f.write(text[:4000] + "\n")
            except Exception:
                pass

        return events

    def _try_parse_event_json(self, s):
        if not s or not s.strip():
            return None
        s = _strip_json_comments(_normalize_json_text(s.strip()))

        ev = None
        try:
            ev = json.loads(s)
        except json.JSONDecodeError:
            return None
        if not isinstance(ev, dict):
            return None

        for wrapper in ("event", "calendar", "data", "result", "payload"):
            if wrapper in ev and isinstance(ev[wrapper], dict):
                inner = ev[wrapper]
                if any(k in inner for k in ("title", "summary", "name", "start", "start_ts")):
                    ev = inner
                    break

        def pick(*keys):
            for k in keys:
                if k in ev and ev[k] not in (None, "", "null"):
                    return ev[k]
            return None

        title = pick("title", "summary", "name", "event", "subject")
        start = pick("start_ts", "start", "start_time", "startTime",
                     "datetime", "date_start", "start_datetime", "from")
        end = pick("end_ts", "end", "end_time", "endTime",
                   "date_end", "end_datetime", "to")
        desc = pick("description", "desc", "details", "notes", "comment") or ""

        if not (title and start and end):
            return None

        s_norm = _parse_ts_relaxed(start)
        e_norm = _parse_ts_relaxed(end)
        if not s_norm or not e_norm:
            return None

        return {
            "title": str(title).strip(),
            "start_ts": s_norm,
            "end_ts": e_norm,
            "description": str(desc).strip(),
        }

    def _render_calendar_suggestions(self, events):
        self.chat_display.config(state=tk.NORMAL)
        container = tk.Frame(self.chat_display, bg="#1e1e1e")
        for ev in events:
            card = tk.Frame(container, bg="#252526", highlightthickness=2,
                            highlightbackground="#0e639c", padx=10, pady=10)
            card.pack(fill="x", pady=6, padx=8)
            tk.Label(card, text=f"📅 {ev['title']}", bg="#252526", fg="#4ec9b0",
                     font=("Segoe UI", 10, "bold"), anchor="w").pack(fill="x")
            tk.Label(card, text=f"{ev['start_ts']} → {ev['end_ts']}", bg="#252526",
                     fg="#d4d4d4", font=("Consolas", 9), anchor="w").pack(fill="x", pady=(2, 0))
            if ev.get("description"):
                tk.Label(card, text=ev["description"], bg="#252526", fg="#aaa",
                         font=("Segoe UI", 9), anchor="w", wraplength=500,
                         justify="left").pack(fill="x", pady=(2, 0))
            btn = tk.Button(card, text="  ➕  Add to Calendar  ",
                            command=lambda e=ev: self._add_suggested_event(e),
                            bg="#0e639c", fg="white",
                            activebackground="#1177bb", activeforeground="white",
                            font=("Segoe UI", 10, "bold"), relief="flat",
                            cursor="hand2", padx=12, pady=6)
            btn.pack(anchor="e", pady=(10, 0))
        self.chat_display.window_create(tk.END, window=container)
        self.chat_display.insert(tk.END, "\n\n")
        self.chat_display.see(tk.END)
        self.chat_display.config(state=tk.DISABLED)

    def _add_suggested_event(self, ev):
        try:
            with _db() as conn:
                conn.execute(
                    "INSERT INTO events (title, start_ts, end_ts, description) VALUES (?, ?, ?, ?)",
                    (ev["title"],
                     _parse_ts_relaxed(ev["start_ts"]) or ev["start_ts"],
                     _parse_ts_relaxed(ev["end_ts"]) or ev["end_ts"],
                     ev.get("description", "")))
            self.calendar_tab.refresh()
            self.flash_status(f"Status: Added '{ev['title']}' to calendar", "green")
            self.conversation_history.append({
                "role": "assistant",
                "content": f"[System note: Event confirmed and saved — "
                           f"'{ev['title']}' at {ev['start_ts']} to {ev['end_ts']}]"
            })
        except Exception as e:
            self.flash_status(f"Status: Failed to add event: {e}", "red")


if __name__ == "__main__":
    root = tk.Tk()
    app = LocalAIApp(root)
    root.mainloop()