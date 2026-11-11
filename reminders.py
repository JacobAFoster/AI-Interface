"""Notification config, ntfy sender, and background reminder service."""
import os
import json
import threading
import urllib.request
from datetime import datetime, timedelta

from .db import (
    NOTIFIER_CONFIG, NTFY_URL, _db, _add_month, _advance_recurrence,
)


def _default_notifier_config():
    return {"enabled": False, "ntfy_topic": "", "lead_minutes": [15, 60]}


def _load_notifier_config():
    cfg = _default_notifier_config()
    try:
        if os.path.exists(NOTIFIER_CONFIG):
            with open(NOTIFIER_CONFIG, "r", encoding="utf-8") as f:
                loaded = json.load(f)
            for k in cfg:
                if k in loaded: cfg[k] = loaded[k]
            if "notified_keys" in loaded: cfg["notified_keys"] = loaded["notified_keys"]
    except Exception as e:
        print(f"Notifier config load error: {e}")
    return cfg


def _save_notifier_config(cfg):
    try:
        with open(NOTIFIER_CONFIG, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2)
    except Exception as e:
        print(f"Notifier config save error: {e}")


def _ntfy_title(s):
    if not s:
        return ""
    s = str(s).replace("\r", " ").replace("\n", " ")
    return s.replace("<", "\u2039").replace(">", "\u203a")


def _send_ntfy(topic, title, message, priority="default", tags="calendar"):
    if not topic: return False
    prio_map = {"min": 1, "low": 2, "default": 3, "high": 4, "urgent": 5}
    priority_val = prio_map.get(priority, 3) if isinstance(priority, str) else priority

    payload = json.dumps({
        "topic": topic,
        "title": _ntfy_title(title),
        "message": message,
        "priority": priority_val,
        "tags": [tags] if isinstance(tags, str) else (tags or []),
    }).encode("utf-8")

    req = urllib.request.Request(
        NTFY_URL, data=payload, method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return 200 <= resp.status < 300
    except Exception as e:
        print(f"ntfy send failed: {e}")
        return False


class ReminderService:
    def __init__(self, app):
        self.app = app
        self._stop = threading.Event()
        self._thread = None
        self._notified = set()
        self._lock = threading.Lock()
        self._load_notified()

    def _load_notified(self):
        saved = self.app.notifier_config.get("notified_keys", [])
        with self._lock:
            for k in saved:
                try:
                    if len(k) == 2: self._notified.add(("event", int(k[0]), int(k[1])))
                    elif len(k) == 3: self._notified.add((str(k[0]), int(k[1]), int(k[2])))
                except Exception: pass

    def _persist_notified(self):
        with self._lock:
            keys = [list(k) for k in self._notified]
        self.app.notifier_config["notified_keys"] = keys
        _save_notifier_config(self.app.notifier_config)

    def _prune_stale_keys(self):
        try:
            with _db() as conn:
                event_ids = set(r[0] for r in conn.execute("SELECT id FROM events").fetchall())
                task_ids = set(r[0] for r in conn.execute("SELECT id FROM tasks").fetchall())
        except Exception: return
        with self._lock:
            pruned = {
                (kind, iid, lead) for (kind, iid, lead) in self._notified
                if (kind == "event" and iid in event_ids) or (kind == "task" and iid in task_ids)
            }
            changed = pruned != self._notified
            self._notified = pruned
        if changed: self._persist_notified()

    def start(self):
        if self._thread and self._thread.is_alive(): return
        self._prune_stale_keys()
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self): self._stop.set()

    def reset_notified(self, persist=True):
        """Forget which reminders were sent. With persist=False only memory and the
        in-memory config are updated; the caller is expected to save the config
        itself (e.g. debounced), so we don't hit the disk on every keystroke."""
        had = False
        with self._lock:
            had = bool(self._notified)
            self._notified.clear()
        if had:
            self.app.notifier_config["notified_keys"] = []
            if persist:
                _save_notifier_config(self.app.notifier_config)

    def clear_notified_for(self, kind, item_id):
        """Forget sent reminders for one event/task (it was rescheduled or recurred)."""
        with self._lock:
            keep = {k for k in self._notified if not (k[0] == kind and k[1] == item_id)}
            changed = keep != self._notified
            self._notified = keep
        if changed:
            self._persist_notified()

    def _post_to_ui(self, fn):
        """Hand a callback to the Tk main loop, preferring an app-provided queue."""
        try:
            q = getattr(self.app, "ui_queue", None)
            if q is not None:
                q.put(fn)
                return
        except Exception:
            pass
        # Fallback: direct call. Prefer apps to supply ui_queue for full thread-safety.
        try:
            self.app.root.after(0, fn)
        except Exception:
            pass

    def _advance_recurring_events(self):
        now_dt = datetime.now()
        now_str = now_dt.strftime("%Y-%m-%dT%H:%M")
        advanced_ids = []
        with _db() as conn:
            rows = conn.execute(
                "SELECT id, start_ts, end_ts, recurrence FROM events "
                "WHERE recurrence != '' AND end_ts < ?",
                (now_str,)
            ).fetchall()
            for r in rows:
                try:
                    start = datetime.strptime(r["start_ts"], "%Y-%m-%dT%H:%M")
                    end = datetime.strptime(r["end_ts"], "%Y-%m-%dT%H:%M")
                    rec = r["recurrence"]
                    if rec not in ("daily", "weekly", "monthly"):
                        continue
                    iterations = 0
                    advanced = None
                    while end < now_dt and iterations < 1000:
                        advanced = _advance_recurrence(start, end, rec)
                        if advanced is None:
                            break
                        start, end = advanced
                        iterations += 1
                    if advanced is None and iterations == 0:
                        continue
                    conn.execute("UPDATE events SET start_ts=?, end_ts=? WHERE id=?",
                                 (start.strftime("%Y-%m-%dT%H:%M"),
                                  end.strftime("%Y-%m-%dT%H:%M"), r["id"]))
                    advanced_ids.append(r["id"])
                except Exception:
                    pass
        # The occurrence moved: reminders sent for the old one must not suppress the next.
        for eid in advanced_ids:
            self.clear_notified_for("event", eid)

    def _loop(self):
        while not self._stop.is_set():
            try:
                self._advance_recurring_events()
                self._check()
            except Exception as e:
                print(f"Reminder check error: {e}")
            self._stop.wait(30)

    def _check(self):
        cfg = self.app.notifier_config
        if not cfg.get("enabled") or not cfg.get("ntfy_topic"): return
        now = datetime.now()
        now_str = now.strftime("%Y-%m-%dT%H:%M")
        global_leads = cfg.get("lead_minutes") or [15]

        with _db() as conn:
            event_rows = conn.execute(
                "SELECT id, title, start_ts, end_ts, description, reminder_minutes "
                "FROM events WHERE start_ts > ? ORDER BY start_ts", (now_str,)
            ).fetchall()
            task_rows = conn.execute(
                "SELECT id, title, due_ts, description, priority, reminder_minutes "
                "FROM tasks WHERE done = 0 AND due_ts IS NOT NULL AND due_ts > ? "
                "ORDER BY due_ts", (now_str,)
            ).fetchall()

        for row in event_rows:
            ev = dict(row)
            try: start = datetime.strptime(ev["start_ts"], "%Y-%m-%dT%H:%M")
            except ValueError: continue

            rm = ev.get("reminder_minutes")
            leads = [rm] if rm is not None else list(global_leads)

            for lead in leads:
                if lead <= 0: continue
                key = ("event", ev["id"], lead)
                with self._lock:
                    if key in self._notified: continue
                trigger = start - timedelta(minutes=lead)
                if now < trigger: continue
                remaining = int((start - now).total_seconds() // 60)
                if remaining <= 0: continue

                title = f"⏰ {ev['title']}"
                body = f"Starts at {start.strftime('%H:%M')} — in {remaining} min"
                if ev.get("description"): body += f"\n{ev['description']}"

                if _send_ntfy(cfg["ntfy_topic"], title, body):
                    with self._lock: self._notified.add(key)
                    self._persist_notified()
                    self._post_to_ui(
                        lambda t=ev["title"]: self.app.flash_status(
                            f"🔔 Event reminder sent: {t}", "green", 3000))

        for row in task_rows:
            task = dict(row)
            rm = task.get("reminder_minutes")
            if rm is None or rm <= 0: continue
            try: due = datetime.strptime(task["due_ts"], "%Y-%m-%dT%H:%M")
            except ValueError: continue

            key = ("task", task["id"], rm)
            with self._lock:
                if key in self._notified: continue
            trigger = due - timedelta(minutes=rm)
            if now < trigger: continue
            remaining = int((due - now).total_seconds() // 60)
            if remaining <= 0: continue

            title = f"📋 Task reminder: {task['title']}"
            prio_tag = task.get("priority") or "normal"
            body = f"Due at {due.strftime('%H:%M')} — in {remaining} min"
            if task.get("description"): body += f"\n{task['description']}"

            if _send_ntfy(cfg["ntfy_topic"], title, body,
                          priority="high" if prio_tag == "high" else "default",
                          tags="clipboard"):
                with self._lock: self._notified.add(key)
                self._persist_notified()
                self._post_to_ui(
                    lambda t=task["title"]: self.app.flash_status(
                        f"📋 Task reminder sent: {t}", "green", 3000))