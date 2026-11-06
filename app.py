import os
import json
import sqlite3
import subprocess
import threading
import urllib.request
import urllib.error
from datetime import datetime, timedelta
from contextlib import contextmanager
from flask import Flask, request, Response, jsonify, session, render_template_string

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET", os.urandom(24).hex())

OLLAMA_HOST = os.environ.get("OLLAMA_HOST", "http://127.0.0.1:11434")
MAX_HISTORY = 20
APP_PASSWORD = os.environ.get("APP_PASSWORD", "")

# --- Personalities --------------------------------------------------------
def _load_personalities():
    path = "personalities.txt"
    defaults = {
        "Seasoned Tech Lead": "You are an elite veteran software engineer and technical lead. Focus on architectural best practices, clean code, performance, scalability, security, and robust error handling.",
        "Startup Founder": "You are a pragmatic, growth-driven startup founder and serial entrepreneur. Focus on MVPs, monetization strategies, rapid execution, and market validation.",
        "Deep Research Analyst": "You are an elite research scientist and investigative analyst. Focus on uncovering comprehensive facts, synthesizing complex information from multiple angles, examining historical and technical context, maintaining strict objectivity, and structuring findings into clear, exhaustive reports with rigorous detail."
    }
    try:
        if not os.path.exists(path):
            with open(path, "w", encoding="utf-8") as f:
                for n, p in defaults.items():
                    f.write(f"{n} = {p}\n")
        result = {}
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    result[k.strip()] = v.strip()
        return result or defaults
    except Exception as e:
        print(f"Personalities load error: {e}")
        return defaults

PERSONALITIES = _load_personalities()

# --- Session-scoped conversation memory -----------------------------------
_sessions = {}
_sessions_lock = threading.Lock()

def _get_history():
    if "sid" not in session:
        session["sid"] = os.urandom(16).hex()
    with _sessions_lock:
        return _sessions.setdefault(session["sid"], [])

def _save_history(h):
    sid = session.get("sid")
    with _sessions_lock:
        _sessions[sid] = h[-MAX_HISTORY:]

# --- Calendar DB (NEW) ----------------------------------------------------
CALENDAR_DB = os.environ.get("CALENDAR_DB", "calendar.db")

def _init_calendar_db():
    with sqlite3.connect(CALENDAR_DB) as conn:
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
        conn.commit()

_init_calendar_db()

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

def _normalize_ts(s):
    """Normalize to YYYY-MM-DDTHH:MM so lexicographic range queries work."""
    if not s:
        return None
    s = s.strip()
    for fmt in ("%Y-%m-%dT%H:%M", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(s, fmt).strftime("%Y-%m-%dT%H:%M")
        except ValueError:
            continue
    return s

def _calendar_context():
    """Injected into every chat system prompt so the AI knows 'now' and upcoming events."""
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
    return (
        f"\n\n[Calendar context]\n"
        f"Current local time: {now.strftime('%Y-%m-%dT%H:%M')} "
        f"({now.strftime('%A, %B %d, %Y')})\n"
        f"Upcoming events (next 14 days):\n" + "\n".join(lines) + "\n\n"
        "When the user asks you to schedule, propose, or reschedule events, include a "
        "fenced code block tagged 'calendar' containing a single JSON object with keys "
        "title, start_ts, end_ts, description. Use LOCAL time in format YYYY-MM-DDTHH:MM. "
        "Example:\n"
        "```calendar\n"
        '{"title": "Team sync", "start_ts": "2025-01-15T14:00", '
        '"end_ts": "2025-01-15T14:30", "description": "Weekly sync"}\n'
        "```\n"
        "You may emit multiple calendar blocks if the user wants several events. "
        "Always confirm the details in prose too."
    )

# --- Auth -----------------------------------------------------------------
def _require_auth():
    if not APP_PASSWORD:
        return None
    a = request.authorization
    if not a or a.password != APP_PASSWORD:
        return Response("Authentication required", 401,
                        {"WWW-Authenticate": 'Basic realm="Local AI"'})
    return None

# --- Routes ---------------------------------------------------------------
@app.route("/")
def index():
    denied = _require_auth()
    if denied: return denied
    return render_template_string(HTML_TEMPLATE, personalities=list(PERSONALITIES.keys()))

@app.route("/api/models")
def list_models():
    denied = _require_auth()
    if denied: return denied
    try:
        r = subprocess.run(["ollama", "list"], capture_output=True, text=True, check=True)
        lines = r.stdout.strip().split("\n")[1:]
        models = [ln.split()[0] for ln in lines if ln.strip()]
        return jsonify({"models": models})
    except Exception as e:
        return jsonify({"models": [], "error": str(e)}), 500

@app.route("/api/sync", methods=["POST"])
def sync_models():
    denied = _require_auth()
    if denied: return denied
    try:
        mf = "models.txt"
        if not os.path.exists(mf):
            with open(mf, "w", encoding="utf-8") as f:
                f.write("llama3.2:1b\nqwen2.5:1.5b\n")
        with open(mf, "r", encoding="utf-8") as f:
            desired = [l.strip() for l in f if l.strip() and not l.startswith("#")]
        if not desired:
            desired = ["llama3.2:1b"]
        r = subprocess.run(["ollama", "list"], capture_output=True, text=True, check=True)
        installed = [l.split()[0] for l in r.stdout.strip().split("\n")[1:] if l.strip()]
        new_ones = []
        for m in desired:
            if m not in installed:
                try:
                    subprocess.run(["ollama", "pull", m], check=True, capture_output=True)
                    new_ones.append(m)
                except Exception:
                    pass
        return jsonify({"models": desired, "newly_installed": new_ones})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/history")
def get_history_route():
    denied = _require_auth()
    if denied: return denied
    return jsonify({"messages": _get_history()})

@app.route("/api/clear", methods=["POST"])
def clear_history():
    denied = _require_auth()
    if denied: return denied
    _save_history([])
    return jsonify({"ok": True})

# --- Calendar API (NEW) ---------------------------------------------------
@app.route("/api/events")
def list_events():
    denied = _require_auth()
    if denied: return denied
    start = request.args.get("start")
    end = request.args.get("end")
    with _db() as conn:
        if start and end:
            rows = conn.execute(
                "SELECT * FROM events WHERE start_ts < ? AND end_ts > ? ORDER BY start_ts",
                (end, start)
            ).fetchall()
        else:
            rows = conn.execute("SELECT * FROM events ORDER BY start_ts").fetchall()
    return jsonify({"events": [dict(r) for r in rows]})

@app.route("/api/events", methods=["POST"])
def create_event():
    denied = _require_auth()
    if denied: return denied
    data = request.get_json() or {}
    title = (data.get("title") or "").strip()
    start = _normalize_ts(data.get("start_ts"))
    end = _normalize_ts(data.get("end_ts"))
    if not title or not start or not end:
        return jsonify({"error": "title, start_ts, end_ts required"}), 400
    with _db() as conn:
        cur = conn.execute(
            "INSERT INTO events (title, start_ts, end_ts, description) VALUES (?, ?, ?, ?)",
            (title, start, end, (data.get("description") or "").strip())
        )
        ev = dict(conn.execute("SELECT * FROM events WHERE id=?", (cur.lastrowid,)).fetchone())
    return jsonify({"event": ev}), 201

@app.route("/api/events/<int:eid>", methods=["PUT"])
def update_event(eid):
    denied = _require_auth()
    if denied: return denied
    data = request.get_json() or {}
    with _db() as conn:
        row = conn.execute("SELECT * FROM events WHERE id=?", (eid,)).fetchone()
        if not row:
            return jsonify({"error": "not found"}), 404
        conn.execute(
            "UPDATE events SET title=?, start_ts=?, end_ts=?, description=? WHERE id=?",
            (data.get("title", row["title"]),
             _normalize_ts(data.get("start_ts", row["start_ts"])),
             _normalize_ts(data.get("end_ts", row["end_ts"])),
             data.get("description", row["description"]),
             eid)
        )
        ev = dict(conn.execute("SELECT * FROM events WHERE id=?", (eid,)).fetchone())
    return jsonify({"event": ev})

@app.route("/api/events/<int:eid>", methods=["DELETE"])
def delete_event(eid):
    denied = _require_auth()
    if denied: return denied
    with _db() as conn:
        conn.execute("DELETE FROM events WHERE id=?", (eid,))
    return jsonify({"ok": True})

# --- Chat -----------------------------------------------------------------
@app.route("/api/chat", methods=["POST"])
def chat():
    denied = _require_auth()
    if denied: return denied

    data = request.get_json() or {}
    model = data.get("model")
    persona = data.get("persona")
    message = (data.get("message") or "").strip()
    deep = bool(data.get("deep_research"))

    if not model or not message:
        return jsonify({"error": "Missing model or message"}), 400

    system_prompt = PERSONALITIES.get(persona, "")
    system_prompt += _calendar_context()  # NEW: give the AI a clock + calendar

    if deep:
        message = (
            f"Perform an in-depth, comprehensive research report on the following topic: '{message}'.\n\n"
            "Provide a structured breakdown including:\n"
            "1. Executive Summary & Overview\n2. Key Concepts & Core Principles\n"
            "3. Practical Applications & Use Cases\n4. Potential Challenges & Future Outlook\n\n"
            "Write thoroughly and in detail."
        )

    history = _get_history()
    messages = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    messages.extend(history[-MAX_HISTORY:])
    messages.append({"role": "user", "content": message})

    def generate():
        payload = json.dumps({"model": model, "messages": messages, "stream": True}).encode()
        req = urllib.request.Request(
            OLLAMA_HOST + "/api/chat",
            data=payload,
            headers={"Content-Type": "application/json"},
        )
        acc = []
        try:
            with urllib.request.urlopen(req, timeout=600) as resp:
                for raw in resp:
                    if not raw.strip():
                        continue
                    try:
                        chunk = json.loads(raw)
                    except json.JSONDecodeError:
                        continue
                    content = (chunk.get("message") or {}).get("content", "")
                    if content:
                        acc.append(content)
                        yield f"data: {json.dumps({'content': content})}\n\n"
                    if chunk.get("done"):
                        break
            full = "".join(acc).strip()
            if full:
                history.append({"role": "user", "content": message})
                history.append({"role": "assistant", "content": full})
                _save_history(history)
            yield f"data: {json.dumps({'done': True, 'memory': len(_get_history())})}\n\n"
        except urllib.error.URLError as e:
            yield f"data: {json.dumps({'error': f'Cannot reach Ollama: {e}'})}\n\n"
        except Exception as e:
            yield f"data: {json.dumps({'error': str(e)})}\n\n"

    return Response(
        generate(),
        mimetype="text/event-stream",
        headers={"X-Accel-Buffering": "no", "Cache-Control": "no-cache"},
    )

# --- HTML template --------------------------------------------------------
HTML_TEMPLATE = r"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>Local AI Hub</title>
<style>
*{box-sizing:border-box}
body{margin:0;font-family:'Segoe UI',sans-serif;background:#1e1e1e;color:#d4d4d4;height:100vh;overflow:hidden}
#app{display:flex;height:100vh}
#sidebar{width:290px;background:#252526;padding:12px;display:flex;flex-direction:column;gap:8px;overflow-y:auto;border-right:1px solid #333}
#main{flex:1;display:flex;flex-direction:column;padding:12px;min-width:0}
h1{font-size:16px;margin:0 0 4px}
h2{font-size:12px;margin:6px 0 2px;color:#bbb;text-transform:uppercase;letter-spacing:.5px}
button{background:#0e639c;color:#fff;border:none;padding:7px 10px;cursor:pointer;border-radius:3px;font-size:13px;font-family:inherit}
button:hover:not(:disabled){background:#1177bb}
button:disabled{background:#444;cursor:not-allowed}
button.danger{background:#a12a2a}
button.danger:hover:not(:disabled){background:#c33}
.listbox{background:#1e1e1e;color:#d4d4d4;border:1px solid #3c3c3c;border-radius:3px;height:160px;overflow-y:auto;font-family:Consolas,monospace;font-size:12px}
.listbox .item{padding:5px 8px;cursor:pointer}
.listbox .item:hover{background:#2a2d2e}
.listbox .item.selected{background:#094771}
#status{font-size:12px;color:#888;font-style:italic;margin:4px 0 6px}
#chat{flex:1;overflow-y:auto;background:#1e1e1e;border:1px solid #333;border-radius:3px;padding:10px;font-family:Consolas,monospace;font-size:13px;word-wrap:break-word;user-select:text}
.msg-header{margin-top:12px;font-weight:bold;font-family:'Segoe UI',sans-serif}
.msg-header.user{color:#4ec9b0}
.msg-header.ai{color:#c586c0}
.msg-body{margin:4px 0 0 10px;white-space:pre-wrap}
.msg-body.user{background:#252526;padding:6px 10px;border-radius:3px}
.msg-body.error{color:#f48771}
#input-row{display:flex;gap:8px;margin-top:8px}
#input{flex:1;background:#1e1e1e;color:#d4d4d4;border:1px solid #3c3c3c;border-radius:3px;padding:8px;font-family:Consolas,monospace;font-size:13px;resize:vertical;min-height:72px}
#buttons{display:flex;flex-direction:column;gap:6px}
#buttons button{width:145px}
#memory{font-size:11px;color:#888;font-style:italic;margin-top:4px}

/* --- Tabs --- */
#tabs{display:flex;margin-bottom:8px;border-bottom:1px solid #333}
.tab-btn{background:transparent;color:#888;padding:10px 16px;border-bottom:2px solid transparent;border-radius:0;font-size:13px}
.tab-btn:hover{color:#d4d4d4;background:transparent}
.tab-btn.active{color:#fff;border-bottom-color:#0e639c}
.tab-content{display:none;flex-direction:column;flex:1;min-height:0}
.tab-content.active{display:flex}

/* --- Calendar --- */
#cal-wrap{display:flex;gap:12px;flex:1;min-height:0}
#cal-main{flex:1;display:flex;flex-direction:column;min-width:0}
#cal-toolbar{display:flex;gap:8px;align-items:center;margin-bottom:8px}
#cal-title{font-size:16px;font-weight:bold;flex:1}
#cal-grid{display:grid;grid-template-columns:repeat(7,1fr);grid-auto-rows:1fr;gap:1px;background:#333;border:1px solid #333;border-radius:3px;flex:1;min-height:320px;overflow:hidden}
.cal-header-cell{background:#252526;padding:6px;text-align:center;font-size:11px;color:#888;text-transform:uppercase;letter-spacing:.5px}
.cal-cell{background:#1e1e1e;padding:4px 5px;font-size:11px;cursor:pointer;overflow:hidden;display:flex;flex-direction:column;gap:2px;min-height:0}
.cal-cell:hover{background:#2a2d2e}
.cal-cell.cal-other-month{color:#555}
.cal-cell.cal-today .cal-day-num{background:#0e639c;color:#fff;border-radius:50%}
.cal-cell.cal-selected{background:#094771}
.cal-day-num{font-size:11px;width:20px;height:20px;display:flex;align-items:center;justify-content:center}
.cal-event-pill{background:#0e639c;color:#fff;font-size:10px;padding:1px 4px;border-radius:2px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.cal-event-more{font-size:9px;color:#888}
#cal-side{width:280px;display:flex;flex-direction:column;gap:8px;min-height:0}
#cal-selected-date{font-size:13px;font-weight:bold;color:#4ec9b0}
#cal-event-list{flex:1;overflow-y:auto;background:#252526;border-radius:3px;padding:8px;font-size:12px}
.cal-empty{color:#666;font-style:italic}
.cal-event-row{border-left:3px solid #0e639c;padding:6px 8px;margin-bottom:6px;background:#1e1e1e;border-radius:2px}
.cal-event-time{font-size:10px;color:#888}
.cal-event-title{font-weight:bold;margin:2px 0}
.cal-event-desc{color:#aaa;font-size:11px;margin:2px 0;white-space:pre-wrap}
.cal-event-actions{display:flex;gap:4px;margin-top:4px}
.mini-btn{padding:2px 6px;font-size:10px;background:#333}
.mini-btn:hover{background:#444}
.mini-btn.danger{background:#a12a2a}
.mini-btn.danger:hover{background:#c33}

/* --- Modal --- */
#event-modal{position:fixed;inset:0;background:rgba(0,0,0,0.6);display:none;align-items:center;justify-content:center;z-index:100}
#event-modal.open{display:flex}
.modal-box{background:#252526;padding:20px;border-radius:4px;width:420px;max-width:92vw;display:flex;flex-direction:column;gap:10px}
.modal-box h3{margin:0 0 4px;font-size:14px}
.modal-box label{font-size:11px;color:#888;display:block;margin-bottom:3px;text-transform:uppercase}
.modal-box input,.modal-box textarea{width:100%;background:#1e1e1e;color:#d4d4d4;border:1px solid #3c3c3c;border-radius:3px;padding:6px;font-family:inherit;font-size:12px}
.modal-box textarea{resize:vertical;min-height:60px}
.modal-actions{display:flex;gap:8px;justify-content:flex-end;margin-top:8px}

/* --- AI calendar suggestion card --- */
.cal-suggestion{background:#252526;border:1px solid #0e639c;border-radius:3px;padding:8px 10px;margin:8px 0;font-size:12px}
.cal-suggestion .suggestion-title{color:#4ec9b0;font-weight:bold;margin-bottom:2px}
</style></head><body>
<div id="app">
  <div id="sidebar">
    <h2>🤖 Models</h2>
    <div class="listbox" id="model-list"></div>
    <button onclick="syncModels()">🔄 Sync &amp; Reload Models</button>
    <h2>💼 Professional Role</h2>
    <div class="listbox" id="persona-list"></div>
    <h2>⚙️ Session</h2>
    <button class="danger" onclick="clearConversation()">🗑️ Clear Conversation</button>
    <div id="memory">Memory: 0 messages</div>
  </div>
  <div id="main">
    <div id="tabs">
      <button class="tab-btn active" data-tab="chat" onclick="showTab('chat')">💬 Chat</button>
      <button class="tab-btn" data-tab="calendar" onclick="showTab('calendar')">📅 Calendar</button>
    </div>

    <!-- Chat tab -->
    <div id="tab-chat" class="tab-content active">
      <h1>💻 Dev &amp; Business Terminal</h1>
      <div id="status">Status: Idle</div>
      <div id="chat"></div>
      <div id="input-row">
        <textarea id="input" placeholder="Type your prompt here... (Ctrl+Enter to send)"></textarea>
        <div id="buttons">
          <button id="send-btn" onclick="sendMessage(false)">Execute 🚀</button>
          <button id="research-btn" onclick="sendMessage(true)">Deep Research 🔍</button>
        </div>
      </div>
    </div>

    <!-- Calendar tab -->
    <div id="tab-calendar" class="tab-content">
      <div id="cal-wrap">
        <div id="cal-main">
          <div id="cal-toolbar">
            <button onclick="calPrev()">◀</button>
            <div id="cal-title"></div>
            <button onclick="calNext()">▶</button>
            <button onclick="calToday()">Today</button>
            <button onclick="openNewEvent()">+ New Event</button>
          </div>
          <div id="cal-grid"></div>
        </div>
        <div id="cal-side">
          <div id="cal-selected-date"></div>
          <div id="cal-event-list"></div>
        </div>
      </div>
    </div>
  </div>
</div>

<!-- Event modal -->
<div id="event-modal">
  <div class="modal-box">
    <h3 id="event-modal-title">New Event</h3>
    <input type="hidden" id="event-id" />
    <div><label>Title</label><input id="event-title" /></div>
    <div><label>Start</label><input type="datetime-local" id="event-start" /></div>
    <div><label>End</label><input type="datetime-local" id="event-end" /></div>
    <div><label>Description</label><textarea id="event-description"></textarea></div>
    <div class="modal-actions">
      <button onclick="closeEventModal()" style="background:#444">Cancel</button>
      <button onclick="saveEvent()">Save</button>
    </div>
  </div>
</div>

<script>
const PERSONALITIES = {{ personalities|tojson }};
let selectedModel = null;
let selectedPersona = PERSONALITIES[0] || null;
let busy = false;
const $ = id => document.getElementById(id);

function escapeHtml(s) {
  return String(s == null ? '' : s).replace(/[&<>"']/g, c => (
    {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]
  ));
}

// --- Tabs ---
function showTab(name) {
  document.querySelectorAll('.tab-content').forEach(el => el.classList.remove('active'));
  document.querySelectorAll('.tab-btn').forEach(el => el.classList.remove('active'));
  document.getElementById('tab-' + name).classList.add('active');
  document.querySelector('.tab-btn[data-tab="' + name + '"]').classList.add('active');
  if (name === 'calendar') loadEvents();
}

// --- Chat (unchanged core) ---
function renderList(container, items, onSelect) {
  container.innerHTML = '';
  items.forEach(item => {
    const div = document.createElement('div');
    div.className = 'item';
    div.textContent = item;
    div.onclick = () => {
      container.querySelectorAll('.item').forEach(x => x.classList.remove('selected'));
      div.classList.add('selected');
      onSelect(item);
    };
    container.appendChild(div);
  });
}
function selectFirst(container) {
  const f = container.querySelector('.item');
  if (f) f.classList.add('selected');
  return f ? f.textContent : null;
}
function setStatus(text, color) { const s = $('status'); s.textContent = text; s.style.color = color || '#888'; }
function appendUser(text) {
  const chat = $('chat');
  const h = document.createElement('div'); h.className = 'msg-header user'; h.textContent = '[You]';
  const b = document.createElement('div'); b.className = 'msg-body user'; b.textContent = text;
  chat.append(h, b); chat.scrollTop = chat.scrollHeight;
}
let currentAiText = '';
function startAiBlock(label) {
  const chat = $('chat');
  const h = document.createElement('div'); h.className = 'msg-header ai'; h.textContent = '[' + (label || 'AI') + ']';
  const b = document.createElement('div'); b.className = 'msg-body ai'; b.id = 'current-ai';
  chat.append(h, b); chat.scrollTop = chat.scrollHeight;
  currentAiText = '';
}
function appendAiChunk(text) {
  const b = $('current-ai'); if (!b) return;
  b.textContent += text; currentAiText += text;
  $('chat').scrollTop = $('chat').scrollHeight;
}
function appendError(text) {
  const chat = $('chat');
  const b = document.createElement('div'); b.className = 'msg-body error'; b.textContent = '⚠️ ' + text;
  chat.appendChild(b); chat.scrollTop = chat.scrollHeight;
}
function updateMemory(n) { $('memory').textContent = 'Memory: ' + n + ' messages'; }

async function loadModels() {
  try {
    const r = await fetch('/api/models'); const d = await r.json();
    if (d.error) return setStatus('Error: ' + d.error, 'red');
    if (!d.models.length) { $('model-list').innerHTML = '<div class="item">No models installed</div>'; return; }
    renderList($('model-list'), d.models, m => { selectedModel = m; });
    selectedModel = selectFirst($('model-list'));
  } catch (e) { setStatus('Cannot reach server', 'red'); }
}
async function syncModels() {
  setStatus('Syncing models (may take a while)...', 'orange');
  $('model-list').innerHTML = '<div class="item">Checking...</div>';
  try {
    const r = await fetch('/api/sync', {method: 'POST'}); const d = await r.json();
    if (d.error) return setStatus('Sync error: ' + d.error, 'red');
    await loadModels();
    setStatus('Sync complete' + (d.newly_installed?.length ? ' (' + d.newly_installed.length + ' new)' : ''), 'green');
  } catch (e) { setStatus('Sync failed', 'red'); }
}
async function clearConversation() {
  if (!confirm('Clear conversation and terminal display?')) return;
  await fetch('/api/clear', {method: 'POST'});
  $('chat').innerHTML = '';
  updateMemory(0);
  setStatus('Conversation cleared', 'green');
}
async function sendMessage(deep) {
  if (busy) return;
  const text = $('input').value.trim();
  if (!text) return;
  if (!selectedModel) return setStatus('No model selected', 'red');
  $('input').value = '';
  appendUser((deep ? '🔍 ' : '') + text);
  startAiBlock('AI (' + selectedModel + ' | ' + selectedPersona + ')');
  busy = true; $('send-btn').disabled = true; $('research-btn').disabled = true;
  const t0 = Date.now();
  const timer = setInterval(() => {
    setStatus('🧠 Thinking... (' + Math.floor((Date.now()-t0)/1000) + 's)', 'orange');
  }, 500);
  try {
    const resp = await fetch('/api/chat', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({model: selectedModel, persona: selectedPersona, message: text, deep_research: deep})
    });
    if (!resp.ok) { appendError('Server error: ' + resp.status); return; }
    const reader = resp.body.getReader(); const dec = new TextDecoder();
    let buf = '';
    while (true) {
      const {done, value} = await reader.read();
      if (done) break;
      buf += dec.decode(value, {stream: true});
      const lines = buf.split('\n'); buf = lines.pop() || '';
      for (const line of lines) {
        if (!line.startsWith('data: ')) continue;
        let p; try { p = JSON.parse(line.slice(6)); } catch { continue; }
        if (p.content) { clearInterval(timer); setStatus('Receiving...', 'green'); appendAiChunk(p.content); }
        if (p.error) appendError(p.error);
        if (p.done) updateMemory(p.memory);
      }
    }
  } catch (e) { appendError(e.message); }
  finally {
    clearInterval(timer);
    setStatus('Status: Idle', '#888');
    busy = false; $('send-btn').disabled = false; $('research-btn').disabled = false;
    const ai = $('current-ai');
    if (ai) {
      ai.id = '';
      // Replace raw ```calendar blocks with a friendly marker
      if (/```calendar/.test(currentAiText)) {
        ai.textContent = currentAiText.replace(/```calendar\s*\n[\s\S]*?```/g, '\n📅 [Event proposed below]\n');
      }
      parseCalendarSuggestions(currentAiText);
    }
  }
}

// --- AI calendar block parsing ---
function parseCalendarSuggestions(text) {
  const re = /```calendar\s*\n([\s\S]*?)```/g;
  let match;
  while ((match = re.exec(text)) !== null) {
    try {
      const ev = JSON.parse(match[1].trim());
      if (ev.title && ev.start_ts && ev.end_ts) addCalendarSuggestion(ev);
    } catch (e) { /* ignore malformed */ }
  }
}
function addCalendarSuggestion(ev) {
  const chat = $('chat');
  const card = document.createElement('div');
  card.className = 'cal-suggestion';
  card.innerHTML =
    '<div class="suggestion-title">📅 ' + escapeHtml(ev.title) + '</div>' +
    '<div>' + escapeHtml(ev.start_ts) + ' → ' + escapeHtml(ev.end_ts) + '</div>' +
    (ev.description ? '<div class="cal-event-desc">' + escapeHtml(ev.description) + '</div>' : '') +
    '<button class="mini-btn" style="margin-top:6px">Add to Calendar</button>';
  card.querySelector('button').onclick = async () => {
    const r = await fetch('/api/events', {
      method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify(ev)
    });
    const btn = card.querySelector('button');
    if (r.ok) {
      btn.textContent = '✓ Added';
      btn.disabled = true;
      if ($('tab-calendar').classList.contains('active')) loadEvents();
    } else {
      btn.textContent = 'Failed to add';
    }
  };
  chat.appendChild(card);
  chat.scrollTop = chat.scrollHeight;
}

// --- Calendar state ---
let calCurrent = new Date();
calCurrent.setDate(1);
let calEvents = [];
let calSelectedDate = new Date();

function localISO(d) {
  const p = n => String(n).padStart(2,'0');
  return d.getFullYear() + '-' + p(d.getMonth()+1) + '-' + p(d.getDate()) +
         'T' + p(d.getHours()) + ':' + p(d.getMinutes());
}

async function loadEvents() {
  const y = calCurrent.getFullYear(), m = calCurrent.getMonth();
  const start = localISO(new Date(y, m, 1));
  const end = localISO(new Date(y, m + 1, 1));
  try {
    const r = await fetch('/api/events?start=' + encodeURIComponent(start) + '&end=' + encodeURIComponent(end));
    const d = await r.json();
    calEvents = d.events || [];
    renderCalendar();
  } catch (e) {
    $('cal-grid').innerHTML = '<div class="cal-empty" style="grid-column:span 7;padding:20px">Failed to load events</div>';
  }
}

function renderCalendar() {
  const grid = $('cal-grid');
  grid.innerHTML = '';
  ['Sun','Mon','Tue','Wed','Thu','Fri','Sat'].forEach(d => {
    const h = document.createElement('div');
    h.className = 'cal-header-cell';
    h.textContent = d;
    grid.appendChild(h);
  });
  const y = calCurrent.getFullYear(), m = calCurrent.getMonth();
  const firstDay = new Date(y, m, 1);
  const gridStart = new Date(y, m, 1 - firstDay.getDay());
  const today = new Date(); today.setHours(0,0,0,0);

  for (let i = 0; i < 42; i++) {
    const d = new Date(gridStart);
    d.setDate(gridStart.getDate() + i);
    const cell = document.createElement('div');
    cell.className = 'cal-cell';
    if (d.getMonth() !== m) cell.classList.add('cal-other-month');
    if (d.getTime() === today.getTime()) cell.classList.add('cal-today');
    if (d.toDateString() === calSelectedDate.toDateString()) cell.classList.add('cal-selected');

    const num = document.createElement('div');
    num.className = 'cal-day-num';
    num.textContent = d.getDate();
    cell.appendChild(num);

    const dayEvents = calEvents.filter(e => new Date(e.start_ts).toDateString() === d.toDateString());
    dayEvents.slice(0, 3).forEach(e => {
      const pill = document.createElement('div');
      pill.className = 'cal-event-pill';
      pill.textContent = e.title;
      cell.appendChild(pill);
    });
    if (dayEvents.length > 3) {
      const more = document.createElement('div');
      more.className = 'cal-event-more';
      more.textContent = '+' + (dayEvents.length - 3) + ' more';
      cell.appendChild(more);
    }
    cell.onclick = () => {
      calSelectedDate = new Date(d);
      renderCalendar();
    };
    grid.appendChild(cell);
  }
  $('cal-title').textContent = calCurrent.toLocaleDateString('en-US', {month: 'long', year: 'numeric'});
  renderEventList();
}

function renderEventList() {
  const list = $('cal-event-list');
  list.innerHTML = '';
  $('cal-selected-date').textContent =
    calSelectedDate.toLocaleDateString('en-US', {weekday: 'long', month: 'long', day: 'numeric'});
  const dayEvents = calEvents
    .filter(e => new Date(e.start_ts).toDateString() === calSelectedDate.toDateString())
    .sort((a,b) => a.start_ts.localeCompare(b.start_ts));
  if (!dayEvents.length) {
    list.innerHTML = '<div class="cal-empty">No events</div>';
    return;
  }
  dayEvents.forEach(e => {
    const s = new Date(e.start_ts), en = new Date(e.end_ts);
    const row = document.createElement('div');
    row.className = 'cal-event-row';
    row.innerHTML =
      '<div class="cal-event-time">' +
        s.toLocaleTimeString('en-US', {hour:'2-digit', minute:'2-digit'}) + ' – ' +
        en.toLocaleTimeString('en-US', {hour:'2-digit', minute:'2-digit'}) +
      '</div>' +
      '<div class="cal-event-title">' + escapeHtml(e.title) + '</div>' +
      (e.description ? '<div class="cal-event-desc">' + escapeHtml(e.description) + '</div>' : '') +
      '<div class="cal-event-actions">' +
        '<button class="mini-btn" onclick="openEditEvent(' + e.id + ')">Edit</button>' +
        '<button class="mini-btn danger" onclick="deleteEvent(' + e.id + ')">Delete</button>' +
      '</div>';
    list.appendChild(row);
  });
}

function calPrev() { calCurrent.setMonth(calCurrent.getMonth() - 1); loadEvents(); }
function calNext() { calCurrent.setMonth(calCurrent.getMonth() + 1); loadEvents(); }
function calToday() {
  const now = new Date();
  calCurrent = new Date(now.getFullYear(), now.getMonth(), 1);
  calSelectedDate = new Date(now);
  loadEvents();
}

// --- Event modal ---
function openNewEvent() {
  $('event-modal-title').textContent = 'New Event';
  $('event-id').value = '';
  $('event-title').value = '';
  const d = calSelectedDate;
  const start = new Date(d); start.setHours(9, 0, 0, 0);
  const end = new Date(d);   end.setHours(10, 0, 0, 0);
  $('event-start').value = localISO(start);
  $('event-end').value = localISO(end);
  $('event-description').value = '';
  $('event-modal').classList.add('open');
  setTimeout(() => $('event-title').focus(), 30);
}
function openEditEvent(id) {
  const e = calEvents.find(x => x.id === id);
  if (!e) return;
  $('event-modal-title').textContent = 'Edit Event';
  $('event-id').value = e.id;
  $('event-title').value = e.title;
  $('event-start').value = e.start_ts.slice(0, 16);
  $('event-end').value = e.end_ts.slice(0, 16);
  $('event-description').value = e.description || '';
  $('event-modal').classList.add('open');
}
function closeEventModal() { $('event-modal').classList.remove('open'); }
async function saveEvent() {
  const id = $('event-id').value;
  const payload = {
    title: $('event-title').value.trim(),
    start_ts: $('event-start').value,
    end_ts: $('event-end').value,
    description: $('event-description').value.trim()
  };
  if (!payload.title || !payload.start_ts || !payload.end_ts) {
    return alert('Title, start, and end are required');
  }
  const url = id ? '/api/events/' + id : '/api/events';
  const method = id ? 'PUT' : 'POST';
  const r = await fetch(url, {
    method, headers: {'Content-Type':'application/json'}, body: JSON.stringify(payload)
  });
  if (!r.ok) return alert('Save failed');
  closeEventModal();
  loadEvents();
}
async function deleteEvent(id) {
  if (!confirm('Delete this event?')) return;
  await fetch('/api/events/' + id, {method: 'DELETE'});
  loadEvents();
}

// --- Init ---
$('input').addEventListener('keydown', e => {
  if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) { e.preventDefault(); sendMessage(false); }
});
$('event-modal').addEventListener('click', e => {
  if (e.target.id === 'event-modal') closeEventModal();
});
(async () => {
  renderList($('persona-list'), PERSONALITIES, p => { selectedPersona = p; });
  selectedPersona = selectFirst($('persona-list')) || PERSONALITIES[0] || null;
  await loadModels();
  try {
    const r = await fetch('/api/history'); const d = await r.json();
    updateMemory(d.messages.length);
  } catch (e) {}
})();
</script></body></html>
"""

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, threaded=True)