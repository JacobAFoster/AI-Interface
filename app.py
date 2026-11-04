import os
import json
import subprocess
import threading
import urllib.request
import urllib.error
from flask import Flask, request, Response, jsonify, session, render_template_string

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET", os.urandom(24).hex())

# Ollama stays on localhost on the machine running this server
OLLAMA_HOST = os.environ.get("OLLAMA_HOST", "http://127.0.0.1:11434")
MAX_HISTORY = 20

# Optional HTTP Basic Auth: set APP_PASSWORD=something to require a password
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

    if deep:
        message = (
            f"Perform an in-depth, comprehensive research report on the following topic: '{message}'.\n\n"
            "Provide a structured breakdown including:\n"
            "1. Executive Summary & Overview\n"
            "2. Key Concepts & Core Principles\n"
            "3. Practical Applications & Use Cases\n"
            "4. Potential Challenges & Future Outlook\n\n"
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
                    msg = chunk.get("message") or {}
                    content = msg.get("content", "")
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
button{background:#0e639c;color:#fff;border:none;padding:7px 10px;cursor:pointer;border-radius:3px;font-size:13px}
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
</div>
<script>
const PERSONALITIES = {{ personalities|tojson }};
let selectedModel = null;
let selectedPersona = PERSONALITIES[0] || null;
let busy = false;
const $ = id => document.getElementById(id);

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
function startAiBlock(label) {
  const chat = $('chat');
  const h = document.createElement('div'); h.className = 'msg-header ai'; h.textContent = '[' + (label || 'AI') + ']';
  const b = document.createElement('div'); b.className = 'msg-body ai'; b.id = 'current-ai';
  chat.append(h, b); chat.scrollTop = chat.scrollHeight;
}
function appendAiChunk(text) {
  const b = $('current-ai'); if (!b) return;
  b.textContent += text; $('chat').scrollTop = $('chat').scrollHeight;
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
    const ai = $('current-ai'); if (ai) ai.id = '';
  }
}
$('input').addEventListener('keydown', e => {
  if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) { e.preventDefault(); sendMessage(false); }
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
    # 0.0.0.0 so other machines on the LAN can reach it
    app.run(host="0.0.0.0", port=5000, threaded=True)