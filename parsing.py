"""Pure parsing/formatting helpers. No Tk, no DB writes — all unit-testable."""
import os
import re
import json
from datetime import datetime, timedelta

from .db import PARSE_LOG, PARSE_LOG_MAX_BYTES, DEFAULT_REMINDER_MINUTES


# --- Timestamp parsing ---------------------------------------------------
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

    hour, minute, ampm = None, 0, ""
    m = re.search(r'(?:\bat\s+)?\b(\d{1,2})(?::(\d{2}))?\s*(am|pm)\b', low)
    if m:
        hour = int(m.group(1))
        minute = int(m.group(2) or 0)
        ampm = m.group(3).lower()
    else:
        m = re.search(r'(?:\bat\s+)?\b(\d{1,2}):(\d{2})\b', low)
        if m:
            hour = int(m.group(1))
            minute = int(m.group(2))
        else:
            m = re.search(r'\bat\s+(\d{1,2})\b', low)
            if m:
                hour = int(m.group(1))
                minute = 0

    if ampm == "pm" and hour is not None and hour < 12:
        hour += 12
    elif ampm == "am" and hour == 12:
        hour = 0

    if hour is not None and (hour > 23 or minute > 59):
        hour, minute = None, 0

    if base is None and hour is None:
        return None
    dt = (base or now).replace(hour=hour or 0, minute=minute, second=0, microsecond=0)
    return dt.strftime("%Y-%m-%dT%H:%M")


# --- JSON-ish parsing helpers -------------------------------------------
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


def _safe_event_text(s):
    if not s:
        return ""
    s = str(s).replace("\r", " ").replace("\n", " ")
    s = s.replace("<", "\u2039").replace(">", "\u203a")
    return s


def _log_parse_failure(text):
    try:
        if os.path.exists(PARSE_LOG) and os.path.getsize(PARSE_LOG) > PARSE_LOG_MAX_BYTES:
            with open(PARSE_LOG, "r", encoding="utf-8", errors="replace") as f:
                content = f.read()
            half = len(content) // 2
            idx = content.find("=" * 60, half)
            if idx > 0:
                content = content[idx:]
            with open(PARSE_LOG, "w", encoding="utf-8") as f:
                f.write(content)
        with open(PARSE_LOG, "a", encoding="utf-8") as f:
            f.write("=" * 60 + "\n")
            f.write(f"TIMESTAMP: {datetime.now().isoformat()}\n")
            f.write(text[:4000] + "\n")
    except Exception:
        pass


def _parse_reminder_minutes(raw):
    try:
        rm_int = int(raw) if raw is not None else DEFAULT_REMINDER_MINUTES
    except (TypeError, ValueError):
        rm_int = DEFAULT_REMINDER_MINUTES
    if rm_int < 0:
        rm_int = 0
    return rm_int


# --- Event / task JSON parsing -------------------------------------------
def _try_parse_event_json(s):
    if not s or not s.strip():
        return None
    s = _strip_json_comments(_normalize_json_text(s.strip()))

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

    rm_raw = pick("reminder_minutes", "remind", "reminder", "remind_minutes",
                  "minutes_before", "alert_minutes")

    return {
        "title": str(title).strip(),
        "start_ts": s_norm,
        "end_ts": e_norm,
        "description": str(desc).strip(),
        "reminder_minutes": _parse_reminder_minutes(rm_raw),
    }


def _try_parse_task_json(s):
    if not s or not s.strip():
        return None
    s = _strip_json_comments(_normalize_json_text(s.strip()))
    try:
        t = json.loads(s)
    except json.JSONDecodeError:
        return None
    if not isinstance(t, dict):
        return None

    for wrapper in ("task", "data", "result", "payload"):
        if wrapper in t and isinstance(t[wrapper], dict):
            inner = t[wrapper]
            if any(k in inner for k in ("title", "summary", "name", "task")):
                t = inner
                break

    def pick(*keys):
        for k in keys:
            if k in t and t[k] not in (None, "", "null"):
                return t[k]
        return None

    title = pick("title", "summary", "name", "task", "subject")
    desc = pick("description", "desc", "details", "notes") or ""
    due = pick("due_ts", "due", "deadline", "due_date", "when")
    prio = (pick("priority", "prio") or "normal").lower()

    if not title:
        return None
    if prio not in ("low", "normal", "high"):
        prio = "normal"
    due_norm = _parse_ts_relaxed(due) if due else None

    rm_raw = pick("reminder_minutes", "remind", "reminder", "remind_minutes",
                  "minutes_before", "alert_minutes")

    return {
        "title": str(title).strip(),
        "description": str(desc).strip(),
        "due_ts": due_norm,
        "priority": prio,
        "reminder_minutes": _parse_reminder_minutes(rm_raw),
    }


def _extract_calendar_events(text):
    events, seen = [], set()
    text = _normalize_json_text(text)

    def _try(candidate):
        ev = _try_parse_event_json(candidate)
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

    for m in re.finditer(r"```(?!\s*task\b)[^\n]*\n(.*?)```",
                         text, re.DOTALL | re.IGNORECASE):
        block = m.group(1).strip()
        block = _strip_json_comments(block)
        for obj in _find_json_objects(block):
            _try(obj)

    for obj in _find_json_objects(_strip_json_comments(text)):
        _try(obj)

    if (not events and "```task" not in text.lower()
            and any(k in text for k in
                    ('"title"', "'title'", '"start', 'calendar'))):
        _log_parse_failure(text)

    return events


def _extract_task_blocks(text):
    tasks, seen = [], set()
    text = _normalize_json_text(text)

    def _try(candidate):
        t = _try_parse_task_json(candidate)
        if t is None:
            return
        key = (t["title"], t.get("due_ts") or "")
        if key in seen:
            return
        seen.add(key)
        tasks.append(t)

    for m in re.finditer(r"```\s*task\b[^\n]*\n?(.*?)```",
                         text, re.DOTALL | re.IGNORECASE):
        block = m.group(1).strip()
        block = _strip_json_comments(block)
        if block.lower().startswith("json"):
            block = block[4:].lstrip()
        for obj in _find_json_objects(block):
            _try(obj)
    return tasks