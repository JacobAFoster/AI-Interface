"""Notification config, ntfy sender, and background reminder service."""
import os
import json
import threading
import urllib.request
from datetime import datetime, timedelta

from .db import (
    NOTIFIER_CONFIG, NTFY_URL, _db,
)


# --- Notifier config -----------------------------------------------------
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
            if "notified_keys" in loaded:
                cfg["notified_keys"] = loaded["notified_keys"]
    except Exception as e:
        print(f"Notifier config load error: {e}")
    return cfg


def _save_notifier_config(cfg):
    try:
        with open(NOTIFIER_CONFIG, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2)
    except Exception as e:
        print(f"Notifier config save error: {e}")


# --- ntfy sender ---------------------------------------------------------
def _send_ntfy(topic, title, message, priority="default", tags="calendar"):
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
        NTFY_URL,
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


# --- Background reminder service ----------------------------------------
class ReminderService:
    def __init__(self, app):
        self.app = app
        self._stop = threading.Event()
        self._thread = None
        # Keys are (kind, item_id, minutes) where kind is "event" or "task".
        self._notified = set()
        self._lock = threading.Lock()
        self._load_notified()

    def _load_notified(self):
        saved = self.app.notifier_config.get("notified_keys", [])
        with self._lock:
            for k in saved:
                try:
                    if len(k) == 2:
                        # Legacy format: (event_id, lead) → assume event
                        self._notified.add(("event", int(k[0]), int(k[1])))
                    elif len(k) == 3:
                        self._notified.add((str(k[0]), int(k[1]), int(k[2])))
                except Exception:
                    pass

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
        except Exception:
            return
        with self._lock:
            pruned = {
                (kind, iid, lead)
                for (kind, iid, lead) in self._notified
                if (kind == "event" and iid in event_ids)
                or (kind == "task" and iid in task_ids)
            }
            changed = pruned != self._notified
            self._notified = pruned
        if changed:
            self._persist_notified()

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._prune_stale_keys()
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()

    def reset_notified(self):
        had = False
        with self._lock:
            had = bool(self._notified)
            self._notified.clear()
        if had:
            self.app.notifier_config["notified_keys"] = []
            _save_notifier_config(self.app.notifier_config)

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
        now_str = now.strftime("%Y-%m-%dT%H:%M")

        global_leads = cfg.get("lead_minutes") or [15]

        with _db() as conn:
            event_rows = conn.execute(
                "SELECT id, title, start_ts, end_ts, description, reminder_minutes "
                "FROM events WHERE start_ts > ? ORDER BY start_ts",
                (now_str,)
            ).fetchall()
            task_rows = conn.execute(
                "SELECT id, title, due_ts, description, priority, reminder_minutes "
                "FROM tasks WHERE done = 0 AND due_ts IS NOT NULL AND due_ts > ? "
                "ORDER BY due_ts",
                (now_str,)
            ).fetchall()

        # ---- Events ----------------------------------------------------
        for row in event_rows:
            ev = dict(row)
            try:
                start = datetime.strptime(ev["start_ts"], "%Y-%m-%dT%H:%M")
            except ValueError:
                continue

            rm = ev.get("reminder_minutes")
            if rm is None:
                leads = list(global_leads)
            else:
                try:
                    rm_int = int(rm)
                except (TypeError, ValueError):
                    leads = list(global_leads)
                else:
                    if rm_int <= 0:
                        continue
                    leads = [rm_int]

            for lead in leads:
                key = ("event", ev["id"], lead)
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
                    self._persist_notified()
                    self.app.root.after(
                        0,
                        lambda t=ev["title"]: self.app.flash_status(
                            f"🔔 Event reminder sent: {t}", "green", 3000)
                    )

        # ---- Tasks -----------------------------------------------------
        for row in task_rows:
            task = dict(row)
            rm = task.get("reminder_minutes")
            if rm is None:
                continue
            try:
                rm_int = int(rm)
            except (TypeError, ValueError):
                continue
            if rm_int <= 0:
                continue

            try:
                due = datetime.strptime(task["due_ts"], "%Y-%m-%dT%H:%M")
            except ValueError:
                continue

            key = ("task", task["id"], rm_int)
            with self._lock:
                if key in self._notified:
                    continue

            trigger = due - timedelta(minutes=rm_int)
            if now < trigger:
                continue

            title = f"📋 Task reminder: {task['title']}"
            prio_tag = task.get("priority") or "normal"
            body = f"Due at {due.strftime('%H:%M')} — {rm_int} min from now"
            if task.get("description"):
                body += f"\n{task['description']}"

            if _send_ntfy(cfg["ntfy_topic"], title, body,
                          priority="high" if prio_tag == "high" else "default",
                          tags="clipboard"):
                with self._lock:
                    self._notified.add(key)
                self._persist_notified()
                self.app.root.after(
                    0,
                    lambda t=task["title"]: self.app.flash_status(
                        f"📋 Task reminder sent: {t}", "green", 3000)
                )