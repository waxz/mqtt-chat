#!/usr/bin/env python3
"""
api_server.py  —  OpenAI-Compatible MQTT Proxy  v3.0

• Modern chat UI with markdown, code highlighting, thinking blocks
• Admin debug console with live connection / worker / stats monitoring
• Pressure-aware load balancing across browser-tab workers
• Robust SSE streaming with proper chunk buffering
"""

import json, time, uuid, asyncio, logging, os
from collections import deque
from typing import Optional, List, Dict, AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, StreamingResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import paho.mqtt.client as mqtt
from paho.mqtt.client import CallbackAPIVersion
import uvicorn

# ════════════════════════════════════════════════════════════
# CONFIGURATION
# ════════════════════════════════════════════════════════════

class Config:
    BROKER_HOST   = os.getenv("MQTT_BROKER_HOST", os.getenv("BROKER_HOST", "127.0.0.1"))
    BROKER_PORT   = int(os.getenv("MQTT_BROKER_PORT", os.getenv("BROKER_PORT", "1883")))
    USE_TLS       = os.getenv("MQTT_USE_TLS", "false").lower() in ("1", "true", "yes")
    WS_PATH       = os.getenv("MQTT_WS_PATH", "/mqtt")
    WS_TRANSPORT  = os.getenv("MQTT_TRANSPORT",
                              "websockets" if int(os.getenv("MQTT_BROKER_PORT",
                              os.getenv("BROKER_PORT", "1883"))) in [80, 443, 7860] else "tcp")
    API_HOST       = "0.0.0.0"
    API_PORT       = 8001
    TIMEOUT_SEC    = 180.0
    SESSION_EXPIRY = 45.0
    DEBUG_MODE     = os.getenv("DEBUG_MODE", "false").lower() in ("1", "true", "yes")

config = Config()
logging.basicConfig(
    level=logging.DEBUG if config.DEBUG_MODE else logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("zen-proxy")

# ════════════════════════════════════════════════════════════
# PYDANTIC MODELS
# ════════════════════════════════════════════════════════════

class ChatMessage(BaseModel):
    role: str
    content: str

class ChatCompletionRequest(BaseModel):
    model: str
    messages: List[ChatMessage]
    stream: bool = False
    temperature: float = 1.0

class ChoiceDelta(BaseModel):
    content: Optional[str] = None
    reasoning_content: Optional[str] = None

class ChoiceChunk(BaseModel):
    delta: ChoiceDelta
    finish_reason: Optional[str] = None
    index: int = 0

class ChatCompletionChunk(BaseModel):
    id: str
    object: str = "chat.completion.chunk"
    created: int
    model: str
    choices: List[ChoiceChunk]

# ════════════════════════════════════════════════════════════
# MQTT PROXY ENGINE
# ════════════════════════════════════════════════════════════

class ProxyEngine:
    def __init__(self):
        self.client_id = f"proxy-{uuid.uuid4().hex[:8]}"
        self.workers: Dict[str, Dict] = {}
        self._queues: Dict[str, asyncio.Queue] = {}
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self.connected = False
        self.activity_log: deque = deque(maxlen=200)

        self.stats = dict(
            start_time=time.time(),
            total_requests=0,
            active_streams=0,
            completed=0,
            failed=0,
            total_chunks=0,
            heartbeats_rx=0,
        )

        self.mqtt = mqtt.Client(
            callback_api_version=CallbackAPIVersion.VERSION2,
            client_id=self.client_id,
            transport=config.WS_TRANSPORT,
        )
        if config.USE_TLS:
            self.mqtt.tls_set()
        if config.WS_TRANSPORT == "websockets":
            self.mqtt.ws_set_options(
                path=config.WS_PATH,
                headers={"Sec-WebSocket-Protocol": "mqtt"},
            )
        self.mqtt.on_connect    = self._on_connect
        self.mqtt.on_message    = self._on_message
        self.mqtt.on_disconnect = self._on_disconnect

    # ── MQTT callbacks (run in paho thread) ──────────────────

    def _on_connect(self, client, userdata, flags, rc, props=None):
        if rc == 0:
            self.connected = True
            logger.info("✅  MQTT connected (%s:%s %s)",
                        config.BROKER_HOST, config.BROKER_PORT, config.WS_TRANSPORT)
            client.subscribe("arena-ai/+/response")
            client.subscribe("arena-ai/global/heartbeat")
            self._log("system", "mqtt/connect", "Connected to broker")
        else:
            logger.error("❌  MQTT connect failed rc=%s", rc)

    def _on_disconnect(self, client, userdata, flags, rc, props=None):
        self.connected = False
        logger.warning("⚠️  MQTT disconnected rc=%s — will auto-reconnect", rc)
        self._log("system", "mqtt/disconnect", f"Disconnected rc={rc}")

    def _on_message(self, client, userdata, msg):
        try:
            topic   = msg.topic
            payload = json.loads(msg.payload.decode())

            if topic == "arena-ai/global/heartbeat":
                sid = payload.get("id")
                if sid:
                    self.workers[sid] = dict(
                        last_seen=time.time(),
                        model=payload.get("model", "AI-Worker"),
                        status=payload.get("status", "ready"),
                        pressure=payload.get("pressure", 0),
                    )
                    self.stats["heartbeats_rx"] += 1
                    self._log("heartbeat", topic, f"{sid} p={payload.get('pressure',0)}")
                return

            if topic.endswith("/response"):
                rid = payload.get("id")
                if rid and rid in self._queues and self._loop:
                    self.stats["total_chunks"] += 1
                    self._loop.call_soon_threadsafe(self._queues[rid].put_nowait, payload)
                    self._log("response", topic, f"{rid}")
        except Exception as exc:
            logger.error("Message parse error: %s", exc)

    # ── helpers ──────────────────────────────────────────────

    def _log(self, kind: str, topic: str, summary: str):
        self.activity_log.append(dict(
            ts=time.time(),
            time=time.strftime("%H:%M:%S"),
            kind=kind,
            topic=topic,
            summary=summary,
        ))

    def set_loop(self, loop):
        self._loop = loop

    def get_active_workers(self) -> Dict[str, Dict]:
        now = time.time()
        expired = [s for s, i in self.workers.items()
                   if now - i["last_seen"] >= config.SESSION_EXPIRY]
        for s in expired:
            del self.workers[s]
        return dict(self.workers)

    # ── core chat generator ──────────────────────────────────

    async def chat(self, req: ChatCompletionRequest) -> AsyncGenerator[Dict, None]:
        self.stats["total_requests"] += 1
        self.stats["active_streams"] += 1

        active = self.get_active_workers()
        target = None

        # direct  model:sid  routing
        if ":" in req.model:
            sid = req.model.rsplit(":", 1)[-1]
            if sid in active:
                target = sid

        # least-pressure routing
        if not target:
            cands = [(s, i) for s, i in active.items()
                     if (req.model in i["model"] or req.model == "auto")
                     and i["status"] == "ready"]
            if cands:
                cands.sort(key=lambda x: x[1]["pressure"])
                target = cands[0][0]

        if not target:
            self.stats["active_streams"] -= 1
            self.stats["failed"] += 1
            raise HTTPException(503, "No active workers. Open a Zen Bridge tab.")

        rid = f"req-{uuid.uuid4().hex[:12]}"
        q: asyncio.Queue = asyncio.Queue()
        self._queues[rid] = q

        mqtt_payload = dict(
            id=rid,
            messages=[m.model_dump() for m in req.messages],
            stream=req.stream,
            temperature=req.temperature,
        )
        logger.info("📤  %s → %s (%s)", rid, active[target]["model"], target)
        self._log("request", f"arena-ai/{target}/request", rid)

        try:
            self.mqtt.publish(
                f"arena-ai/{target}/request", json.dumps(mqtt_payload), qos=1
            )
            deadline = time.time() + config.TIMEOUT_SEC
            while True:
                remaining = deadline - time.time()
                if remaining <= 0:
                    self.stats["failed"] += 1
                    raise HTTPException(504, "Worker response timeout")
                try:
                    chunk = await asyncio.wait_for(q.get(), timeout=min(remaining, 30))
                except asyncio.TimeoutError:
                    continue
                yield chunk
                choices = chunk.get("choices", [])
                if (choices and choices[0].get("finish_reason")) \
                        or chunk.get("object") == "chat.completion":
                    self.stats["completed"] += 1
                    break
        except HTTPException:
            raise
        except Exception as exc:
            self.stats["failed"] += 1
            logger.error("Chat error: %s", exc)
            raise HTTPException(502, str(exc))
        finally:
            self.stats["active_streams"] -= 1
            self._queues.pop(rid, None)


engine = ProxyEngine()

# ════════════════════════════════════════════════════════════
# HTML  —  Landing Page
# ════════════════════════════════════════════════════════════

LANDING_HTML = """<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Zen AI Proxy</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;
     background:#050505;color:#e4e4e7;display:flex;align-items:center;justify-content:center;min-height:100vh}
.wrap{text-align:center;max-width:480px;width:100%;padding:20px}
h1{font-size:2rem;margin-bottom:6px}
.sub{color:#71717a;margin-bottom:36px;font-size:.95rem}
.cards{display:flex;gap:16px;justify-content:center;flex-wrap:wrap}
a.c{display:block;background:#111;border:1px solid #222;border-radius:14px;
    padding:32px 44px;text-decoration:none;color:#e4e4e7;transition:.2s}
a.c:hover{border-color:#3b82f6;background:#0a0f1a;transform:translateY(-2px)}
.ci{font-size:2rem;margin-bottom:10px}
.ct{font-weight:600;font-size:1.05rem}
.cd{color:#71717a;font-size:.8rem;margin-top:4px}
#st{margin-top:36px;font-size:.8rem;color:#52525b}
.d{display:inline-block;width:8px;height:8px;border-radius:50%;margin-right:6px;vertical-align:middle}
.d.on{background:#22c55e;box-shadow:0 0 8px #22c55e}
.d.off{background:#ef4444;box-shadow:0 0 8px #ef4444}
</style></head><body>
<div class="wrap">
 <h1>⚡ Zen AI Proxy</h1>
 <p class="sub">OpenAI-Compatible MQTT Bridge</p>
 <div class="cards">
  <a class="c" href="/chat"><div class="ci">💬</div><div class="ct">Chat</div><div class="cd">AI chat interface</div></a>
  <a class="c" href="/admin"><div class="ci">🔧</div><div class="ct">Admin</div><div class="cd">Debug &amp; monitoring</div></a>
 </div>
 <div id="st">checking…</div>
</div>
<script>
fetch('/admin/api/stats').then(r=>r.json()).then(d=>{
 const on=d.connection.connected;
 document.getElementById('st').innerHTML=
  `<span class="d ${on?'on':'off'}"></span>${on?'Connected':'Disconnected'} · `+
  `${d.workers.count} worker(s) · ${d.stats.total_requests} requests`;
}).catch(()=>{document.getElementById('st').innerHTML='<span class="d off"></span>API unreachable'});
</script></body></html>"""

# ════════════════════════════════════════════════════════════
# HTML  —  Modern Chat UI
# ════════════════════════════════════════════════════════════

CHAT_HTML = r"""<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Zen AI Chat</title>

<!-- markdown + highlight + sanitize -->
<script src="https://cdn.jsdelivr.net/npm/marked/marked.min.js"></script>
<link  href="https://cdnjs.cloudflare.com/ajax/libs/highlight.js/11.9.0/styles/github-dark-dimmed.min.css" rel="stylesheet">
<script src="https://cdnjs.cloudflare.com/ajax/libs/highlight.js/11.9.0/highlight.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/dompurify@3/dist/purify.min.js"></script>

<style>
/* ── reset & vars ─────────────────────────────── */
*{box-sizing:border-box;margin:0;padding:0}
:root{
  --bg0:#0a0a0a;--bg1:#111113;--bg2:#1a1a1d;--bg3:#232326;
  --tx:#e4e4e7;--tx2:#a1a1aa;--tx3:#52525b;
  --accent:#3b82f6;--accent2:#2563eb;
  --border:#27272a;--green:#22c55e;--red:#ef4444;--amber:#ca8a04;
  --radius:12px;--maxW:780px;
  --font:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;
  --mono:'SF Mono',SFMono-Regular,Consolas,'Liberation Mono',Menlo,monospace;
}

[data-theme="light"] {
  --bg0:#ffffff;--bg1:#f4f4f5;--bg2:#e4e4e7;--bg3:#d4d4d8;
  --tx:#18181b;--tx2:#3f3f46;--tx3:#71717a;
  --accent:#2563eb;--accent2:#1d4ed8;
  --border:#d4d4d8;--green:#16a34a;--red:#dc2626;--amber:#a16207;
}
[data-theme="light"] .msg-body code{background:#e4e4e7}
[data-theme="light"] .msg-body pre{background:#f4f4f5;border-color:#d4d4d8}
[data-theme="light"] .code-top{background:#e4e4e7;border-color:#d4d4d8}
[data-theme="light"] .msg-body strong{color:#18181b}
[data-theme="light"] .msg-body h1,
[data-theme="light"] .msg-body h2,
[data-theme="light"] .msg-body h3{color:#18181b}
html,body{height:100%}
body{font-family:var(--font);background:var(--bg0);color:var(--tx);display:flex;flex-direction:column;overflow:hidden}

/* ── header ───────────────────────────────────── */
.hdr{display:flex;align-items:center;justify-content:space-between;
     padding:10px 20px;background:var(--bg1);border-bottom:1px solid var(--border);flex-shrink:0;gap:12px}
.hdr-l{display:flex;align-items:center;gap:12px}
.logo{font-weight:700;font-size:1.05rem;white-space:nowrap}
.logo span{color:var(--accent)}
select.ms{background:var(--bg2);color:var(--tx);border:1px solid var(--border);
           border-radius:8px;padding:6px 10px;font-size:.82rem;outline:none;max-width:220px}
.hdr-r{display:flex;gap:6px}
.ib{background:transparent;border:1px solid var(--border);color:var(--tx2);border-radius:8px;
    padding:6px 12px;font-size:.78rem;cursor:pointer;transition:.15s;text-decoration:none;white-space:nowrap}
.ib:hover{background:var(--bg2);color:var(--tx)}
#connDot{display:inline-block;width:7px;height:7px;border-radius:50%;margin-right:5px;vertical-align:middle;
         background:var(--red);box-shadow:0 0 6px var(--red)}
#connDot.on{background:var(--green);box-shadow:0 0 6px var(--green)}

/* ── chat area ────────────────────────────────── */
.chat{flex:1;overflow-y:auto;padding:24px 20px 12px}
.msgs{max-width:var(--maxW);margin:0 auto}
.msg{margin-bottom:28px;animation:fadeUp .25s ease}
@keyframes fadeUp{from{opacity:0;transform:translateY(6px)}to{opacity:1;transform:none}}
.msg-hdr{display:flex;align-items:center;gap:8px;margin-bottom:6px}
.role{font-weight:600;font-size:.85rem}
.role-u{color:var(--accent)}.role-a{color:var(--green)}
.msg-body{font-size:.92rem;line-height:1.75;color:var(--tx);overflow-wrap:break-word}

/* markdown prose */
.msg-body p{margin-bottom:10px}.msg-body p:last-child{margin-bottom:0}
.msg-body ul,.msg-body ol{margin:8px 0 8px 20px}
.msg-body li{margin-bottom:4px}
.msg-body blockquote{border-left:3px solid var(--border);padding-left:14px;color:var(--tx2);margin:10px 0}
.msg-body a{color:var(--accent)}
.msg-body strong{color:#fff}
.msg-body h1,.msg-body h2,.msg-body h3{margin:18px 0 8px;color:#fff}
.msg-body table{border-collapse:collapse;margin:10px 0;width:100%}
.msg-body th,.msg-body td{border:1px solid var(--border);padding:6px 10px;font-size:.85rem;text-align:left}
.msg-body th{background:var(--bg2)}

/* code */
.msg-body code{background:var(--bg2);padding:2px 6px;border-radius:4px;font-size:.83em;font-family:var(--mono)}
.msg-body pre{position:relative;background:var(--bg1);border:1px solid var(--border);border-radius:8px;
              margin:12px 0;overflow:hidden}
.msg-body pre code{display:block;padding:16px;background:transparent;font-size:.82rem;overflow-x:auto;line-height:1.55}
.code-top{display:flex;justify-content:space-between;align-items:center;
          padding:6px 12px;background:var(--bg2);border-bottom:1px solid var(--border);font-size:.72rem;color:var(--tx3)}
.cp-btn{background:none;border:1px solid var(--border);color:var(--tx3);border-radius:4px;
        padding:2px 8px;font-size:.72rem;cursor:pointer;transition:.15s}
.cp-btn:hover{color:var(--tx);border-color:var(--tx3)}

/* thinking block */
.think{border-left:3px solid var(--amber);background:rgba(202,138,4,.06);
       padding:10px 14px;margin:6px 0 10px;border-radius:0 8px 8px 0}
.think-tog{display:flex;align-items:center;gap:6px;cursor:pointer;font-size:.78rem;
           color:var(--amber);user-select:none}
.think-tog svg{transition:transform .2s}
.think-tog.open svg{transform:rotate(90deg)}
.think-body{margin-top:8px;font-size:.82rem;color:var(--tx2);line-height:1.6;white-space:pre-wrap;
            max-height:300px;overflow-y:auto;display:none}
.think-tog.open+.think-body{display:block}

/* welcome */
.welcome{text-align:center;padding:80px 20px;color:var(--tx3)}
.welcome h2{font-size:1.4rem;color:var(--tx2);margin-bottom:6px;font-weight:600}
.welcome p{font-size:.9rem}

/* loading dots */
.ld span{display:inline-block;animation:bk 1.4s infinite both;font-size:1.2rem;color:var(--tx3)}
.ld span:nth-child(2){animation-delay:.2s}.ld span:nth-child(3){animation-delay:.4s}
@keyframes bk{0%,80%,100%{opacity:.15}40%{opacity:1}}

/* error */
.err{color:var(--red);font-size:.88rem;padding:10px 14px;background:rgba(239,68,68,.08);
     border:1px solid rgba(239,68,68,.2);border-radius:8px;margin-top:6px}

/* ── input area ───────────────────────────────── */
.inp{padding:12px 20px 20px;background:var(--bg0);border-top:1px solid var(--border);flex-shrink:0}
.inp-c{max-width:var(--maxW);margin:0 auto}
.inp-w{display:flex;align-items:flex-end;background:var(--bg1);border:1px solid var(--border);
       border-radius:var(--radius);padding:10px 14px;transition:border-color .2s}
.inp-w:focus-within{border-color:var(--accent)}
textarea{flex:1;background:transparent;border:none;color:var(--tx);font-size:.92rem;line-height:1.5;
         resize:none;outline:none;font-family:var(--font);max-height:180px;min-height:24px}
textarea::placeholder{color:var(--tx3)}
.send{background:var(--accent);color:#fff;border:none;border-radius:8px;width:36px;height:36px;
      display:flex;align-items:center;justify-content:center;cursor:pointer;margin-left:8px;
      flex-shrink:0;transition:.15s}
.send:hover{background:var(--accent2)}.send:disabled{opacity:.4;cursor:not-allowed}
.hint{text-align:center;margin-top:6px;font-size:.72rem;color:var(--tx3)}

/* scrollbar */
::-webkit-scrollbar{width:5px}::-webkit-scrollbar-track{background:transparent}
::-webkit-scrollbar-thumb{background:#333;border-radius:3px}

@media(max-width:600px){
 .hdr{padding:8px 12px;gap:8px}.chat{padding:14px 10px}.inp{padding:8px 10px 14px}
 .hdr-r .ib:not(:first-child){display:none}select.ms{max-width:140px}
}
</style>


  <!-- Highlight.js CSS theme -->
  <link rel="stylesheet"
        href="https://cdnjs.cloudflare.com/ajax/libs/highlight.js/11.9.0/styles/default.min.css">

</head>
<body>

<!-- HEADER -->
<div class="hdr">
 <div class="hdr-l">
  <div class="logo">⚡ <span>Zen AI</span></div>
  <select class="ms" id="mSel"><option value="auto">loading…</option></select>
  <span id="connDot" title="Broker"></span>
 </div>
 <div class="hdr-r">
  <button class="ib" onclick="clearChat()">🗑 Clear</button>
  <a class="ib" href="/admin" target="_blank">🔧 Admin</a>
<button class="ib" id="themeBtn" onclick="toggleTheme()">🌙</button>

 </div>
</div>

<!-- MESSAGES -->
<div class="chat" id="chatScroll">
 <div class="msgs" id="msgs">
  <div class="welcome" id="wel"><h2>Zen AI Chat</h2><p>Select a model and start a conversation</p></div>
 </div>
</div>

<!-- INPUT -->
<div class="inp">
 <div class="inp-c">
  <div class="inp-w">
   <textarea id="ta" rows="1" placeholder="Message Zen AI…"></textarea>
   <button class="send" id="sendBtn" title="Send">
    <svg width="16" height="16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
     <path d="M14.5 1.5L6.5 9.5M14.5 1.5L10 14.5 6.5 9.5 1.5 6 14.5 1.5z"/>
    </svg>
   </button>
  </div>
  <div class="hint">Enter to send · Shift + Enter for new line</div>
 </div>
</div>

<script>
const API = window.location.origin;
let history = [];
let busy = false;

/* ── theme toggle ───────────────────────────── */
function toggleTheme(){
  const t = document.documentElement.getAttribute('data-theme') === 'light' ? '' : 'light';
  document.documentElement.setAttribute('data-theme', t);
  document.getElementById('themeBtn').textContent = t === 'light' ? '☀️' : '🌙';
  localStorage.setItem('zen-theme', t);
}
(function(){
  const saved = localStorage.getItem('zen-theme');
  if(saved === 'light'){
    document.documentElement.setAttribute('data-theme','light');
    document.addEventListener('DOMContentLoaded',()=>{
      const b=document.getElementById('themeBtn'); if(b) b.textContent='☀️';
    });
  }
})();

/* ── marked config ──────────────────────────── */
/* ── marked config (works with marked v12+) ─────── */
const renderer = new marked.Renderer();

renderer.code = function({ text, lang, escaped }) {
  let hi;
  const language = lang || 'code';
  try {
    hi = lang && hljs.getLanguage(lang)
      ? hljs.highlight(text, { language: lang }).value
      : hljs.highlightAuto(text).value;
  } catch (e) {
    // Escape HTML manually on fallback
    hi = text.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
  }
  const safeLabel = language.replace(/</g, '&lt;');
  return `<pre><div class="code-top"><span>${safeLabel}</span>`
       + `<button class="cp-btn" data-copy>Copy</button></div>`
       + `<code class="hljs">${hi}</code></pre>`;
};

marked.setOptions({ renderer, breaks: true, gfm: true });

function render(md) {
  const html = marked.parse(md || '');
  return DOMPurify.sanitize(html, {
    ADD_TAGS: ['button'],
    ADD_ATTR: ['data-copy', 'class']
  });
}

/* ── copy-code via event delegation (survives DOMPurify) ── */
document.addEventListener('click', function(e) {
  const btn = e.target.closest('[data-copy]');
  if (!btn) return;
  const code = btn.closest('pre').querySelector('code');
  navigator.clipboard.writeText(code.textContent);
  btn.textContent = 'Copied!';
  setTimeout(() => btn.textContent = 'Copy', 1500);
});


/* ── helpers ─────────────────────────────────── */
const $=s=>document.getElementById(s);
const ta=$('ta'), msgs=$('msgs'), chatScroll=$('chatScroll');

ta.addEventListener('input',()=>{ta.style.height='auto';ta.style.height=Math.min(ta.scrollHeight,180)+'px';});
ta.addEventListener('keydown',e=>{if(e.key==='Enter'&&!e.shiftKey){e.preventDefault();send();}});
$('sendBtn').addEventListener('click',send);

function scroll(){chatScroll.scrollTop=chatScroll.scrollHeight;}

function cpCode(btn){
  const code=btn.closest('pre').querySelector('code');
  navigator.clipboard.writeText(code.textContent);
  btn.textContent='Copied!';setTimeout(()=>btn.textContent='Copy',1500);
}

/* ── fetch models ────────────────────────────── */
async function fetchModels(){
  try{
    const r=await fetch(API+'/v1/models');const d=await r.json();
    const s=$('mSel');const cur=s.value;s.innerHTML='';
    d.data.forEach(m=>{const o=document.createElement('option');o.value=m.id;o.textContent=m.id;s.appendChild(o);});
    if([...s.options].find(o=>o.value===cur))s.value=cur;
  }catch(e){console.warn('model fetch failed',e);}
}

/* ── connection dot ──────────────────────────── */
async function checkConn(){
  try{const r=await fetch(API+'/admin/api/stats');const d=await r.json();
      $('connDot').className=d.connection.connected?'on':'';
  }catch(e){$('connDot').className='';}
}

/* ── create message element ──────────────────── */
function mkMsg(role){
  const w=document.createElement('div');w.className='msg';
  const hdr=document.createElement('div');hdr.className='msg-hdr';
  const r=document.createElement('span');r.className=`role role-${role[0]}`;
  r.textContent=role==='user'?'You':'Assistant';
  hdr.appendChild(r);
  const body=document.createElement('div');body.className='msg-body';
  w.appendChild(hdr);w.appendChild(body);
  return{el:w,body};
}

/* ── clear ───────────────────────────────────── */
function clearChat(){
  history=[];msgs.innerHTML='<div class="welcome" id="wel"><h2>Zen AI Chat</h2><p>Select a model and start a conversation</p></div>';
}

/* ── send message ────────────────────────────── */
async function send(){
  if(busy)return;
  const txt=ta.value.trim();if(!txt)return;
  const wel=$('wel');if(wel)wel.remove();

  // user msg
  const u=mkMsg('user');u.body.textContent=txt;msgs.appendChild(u.el);
  history.push({role:'user',content:txt});
  ta.value='';ta.style.height='auto';scroll();

  // assistant placeholder
  const a=mkMsg('assistant');
  a.body.innerHTML='<span class="ld"><span>●</span><span>●</span><span>●</span></span>';
  msgs.appendChild(a.el);scroll();

  busy=true;$('sendBtn').disabled=true;
  const model=$('mSel').value||'auto';

  try{
    const res=await fetch(API+'/v1/chat/completions',{
      method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({model,messages:history,stream:true})
    });
    if(!res.ok){const e=await res.json().catch(()=>({}));throw new Error(e.detail||res.statusText);}

    const reader=res.body.getReader(), dec=new TextDecoder();
    let buf='',content='',thinking='',thinkEl=null,contentEl=null;

    while(true){
      const{done,value}=await reader.read();
      if(done)break;
      buf+=dec.decode(value,{stream:true});
      const lines=buf.split('\n');buf=lines.pop()||'';

      for(const ln of lines){
        if(!ln.startsWith('data: '))continue;
        const raw=ln.slice(6).trim();
        if(raw==='[DONE]')continue;
        let j;try{j=JSON.parse(raw);}catch(e){continue;}
        if(j.error)throw new Error(j.error);
        const d=j.choices&&j.choices[0]&&j.choices[0].delta;
        if(!d)continue;

        /* thinking */
        if(d.reasoning_content){
          thinking+=d.reasoning_content;
          if(!thinkEl){
            a.body.innerHTML='';
            thinkEl=document.createElement('div');thinkEl.className='think';
            thinkEl.innerHTML=`<div class="think-tog open" onclick="this.classList.toggle('open')">
              <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M9 18l6-6-6-6"/></svg>
              Thinking…</div><div class="think-body"></div>`;
            a.body.appendChild(thinkEl);
          }
          //thinkEl.querySelector('.think-body').textContent=render(thinking);
          //thinkEl.querySelector('.think-body').innerHTML=render(thinking);
          thinkEl.querySelector('.think-body').innerHTML = render(thinking);
        }

        /* content */
        if(d.content){
          content+=d.content;
          if(!contentEl){contentEl=document.createElement('div');contentEl.className='rendered';a.body.appendChild(contentEl);}
          contentEl.innerHTML=render(content);
        }
        scroll();
      }
    }
    // If nothing was rendered (empty response)
    if(!content&&!thinking){a.body.innerHTML='<span style="color:var(--tx3)">(empty response)</span>';}
    history.push({role:'assistant',content});
  }catch(e){
    a.body.innerHTML=`<div class="err">⚠ ${e.message}</div>`;
  }finally{
    busy=false;$('sendBtn').disabled=false;scroll();ta.focus();
  }
}

/* ── init ────────────────────────────────────── */
fetchModels();checkConn();
setInterval(fetchModels,15000);
setInterval(checkConn,8000);
ta.focus();
</script>
</body></html>"""

# ════════════════════════════════════════════════════════════
# HTML  —  Admin Debug Console
# ════════════════════════════════════════════════════════════

ADMIN_HTML = r"""<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Zen Admin Console</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
:root{--bg:#050505;--c1:#0e0e10;--c2:#161618;--c3:#1e1e21;--tx:#d4d4d8;--tx2:#a1a1aa;--tx3:#52525b;
      --bdr:#222;--accent:#3b82f6;--green:#22c55e;--red:#ef4444;--amber:#eab308;
      --font:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;
      --mono:'SF Mono',Consolas,'Liberation Mono',monospace;}
body{font-family:var(--font);background:var(--bg);color:var(--tx);padding:0;min-height:100vh}

/* header */
.top{background:var(--c1);border-bottom:1px solid var(--bdr);padding:14px 24px;
     display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:10px}
.top h1{font-size:1rem;font-weight:700;white-space:nowrap}
.top h1 span{color:var(--accent)}
.top-r{display:flex;align-items:center;gap:14px;font-size:.8rem;color:var(--tx2)}
.dot{display:inline-block;width:9px;height:9px;border-radius:50%;vertical-align:middle;margin-right:5px}
.dot.on{background:var(--green);box-shadow:0 0 8px var(--green)}
.dot.off{background:var(--red);box-shadow:0 0 8px var(--red)}
.lnk{color:var(--accent);text-decoration:none;font-size:.8rem}
.lnk:hover{text-decoration:underline}

/* layout */
.grid{display:grid;grid-template-columns:1fr 1fr;gap:14px;padding:20px 24px;max-width:1200px;margin:0 auto}
.full{grid-column:1/-1}
@media(max-width:700px){.grid{grid-template-columns:1fr;padding:12px}}

/* card */
.card{background:var(--c1);border:1px solid var(--bdr);border-radius:12px;overflow:hidden}
.card-h{padding:12px 16px;border-bottom:1px solid var(--bdr);display:flex;align-items:center;justify-content:space-between}
.card-t{font-size:.7rem;text-transform:uppercase;letter-spacing:.06em;color:var(--tx3);font-weight:600}
.card-b{padding:16px}

/* kv rows */
.kv{display:flex;justify-content:space-between;padding:6px 0;border-bottom:1px solid #111;font-size:.83rem}
.kv:last-child{border:none}
.kv .k{color:var(--tx3)}.kv .v{color:var(--tx);font-family:var(--mono);font-size:.8rem}

/* stat boxes */
.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(120px,1fr));gap:10px}
.stat{background:var(--c2);border-radius:8px;padding:14px;text-align:center}
.stat .n{font-size:1.5rem;font-weight:700;color:#fff;font-family:var(--mono)}
.stat .l{font-size:.68rem;text-transform:uppercase;color:var(--tx3);margin-top:4px;letter-spacing:.04em}

/* table */
table{width:100%;border-collapse:collapse}
th,td{padding:10px 12px;text-align:left;border-bottom:1px solid #111;font-size:.82rem}
th{color:var(--tx3);font-size:.68rem;text-transform:uppercase;letter-spacing:.05em;font-weight:600}
td{color:var(--tx2)}
code{background:var(--c3);color:var(--accent);padding:2px 6px;border-radius:4px;font-size:.78rem;font-family:var(--mono)}
.badge{display:inline-block;padding:2px 8px;border-radius:10px;font-size:.7rem;font-weight:600}
.badge-ok{background:rgba(34,197,94,.12);color:var(--green)}
.badge-busy{background:rgba(234,179,8,.12);color:var(--amber)}
.badge-off{background:rgba(239,68,68,.12);color:var(--red)}
.empty{text-align:center;padding:32px;color:var(--tx3);font-size:.85rem}

/* log */
.log{max-height:350px;overflow-y:auto;font-family:var(--mono);font-size:.78rem}
.log-row{display:flex;gap:10px;padding:5px 0;border-bottom:1px solid #0c0c0c}
.log-row:hover{background:var(--c2)}
.log-t{color:var(--tx3);flex-shrink:0;width:64px}
.log-k{border-radius:4px;padding:1px 6px;font-size:.7rem;flex-shrink:0}
.log-k.heartbeat{background:rgba(34,197,94,.1);color:var(--green)}
.log-k.response{background:rgba(59,130,246,.1);color:var(--accent)}
.log-k.request{background:rgba(168,85,247,.1);color:#a855f7}
.log-k.system{background:rgba(234,179,8,.1);color:var(--amber)}
.log-topic{color:var(--tx3);flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.log-sum{color:var(--tx2);flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}

/* filter bar */
.fbar{display:flex;gap:6px;margin-bottom:10px;flex-wrap:wrap}
.fbtn{background:var(--c2);border:1px solid var(--bdr);color:var(--tx3);border-radius:6px;
      padding:4px 10px;font-size:.72rem;cursor:pointer;transition:.15s}
.fbtn:hover,.fbtn.act{border-color:var(--accent);color:var(--accent)}

/* refresh indicator */
.pulse{display:inline-block;width:6px;height:6px;border-radius:50%;background:var(--accent);
       animation:pulse 2s infinite}
@keyframes pulse{0%,100%{opacity:.3}50%{opacity:1}}
</style></head>
<body>

<div class="top">
 <h1>🔧 <span>Zen</span> Admin Console</h1>
 <div class="top-r">
  <span><span class="dot off" id="dot"></span><span id="connLabel">Connecting…</span></span>
  <span><span class="pulse"></span> Live</span>
  <a class="lnk" href="/chat">← Chat</a>
  <a class="lnk" href="/">Home</a>
 </div>
</div>

<div class="grid">

 <!-- CONNECTION -->
 <div class="card">
  <div class="card-h"><span class="card-t">Connection</span></div>
  <div class="card-b" id="connCard">—</div>
 </div>

 <!-- CONFIG -->
 <div class="card">
  <div class="card-h"><span class="card-t">Configuration</span></div>
  <div class="card-b" id="cfgCard">—</div>
 </div>

 <!-- STATS -->
 <div class="card full">
  <div class="card-h"><span class="card-t">Engine Statistics</span><span id="uptime" style="font-size:.75rem;color:var(--tx3)"></span></div>
  <div class="card-b"><div class="stats" id="statsGrid">—</div></div>
 </div>

 <!-- WORKERS -->
 <div class="card full">
  <div class="card-h"><span class="card-t">Workers</span><span id="wCount" style="font-size:.75rem;color:var(--tx3)">0</span></div>
  <div class="card-b" style="padding:0"><div id="wTable" class="empty">No workers connected</div></div>
 </div>

 <!-- ACTIVITY LOG -->
 <div class="card full">
  <div class="card-h"><span class="card-t">Network Activity</span>
   <button class="fbtn" onclick="clearLog()" style="font-size:.68rem">Clear</button>
  </div>
  <div class="card-b">
   <div class="fbar" id="filters"></div>
   <div class="log" id="logBox"><div class="empty">Waiting for activity…</div></div>
  </div>
 </div>

 <!-- PENDING QUEUES -->
 <div class="card full">
  <div class="card-h"><span class="card-t">Pending Request Queues</span><span id="qCount" style="font-size:.75rem;color:var(--tx3)">0</span></div>
  <div class="card-b"><div id="qInfo" class="empty" style="padding:16px">None</div></div>
 </div>

</div>

<script>
const API=window.location.origin;
let filter='all';
let logData=[];

function fmt(s){const h=Math.floor(s/3600),m=Math.floor(s%3600/60),ss=Math.floor(s%60);return `${h}h ${m}m ${ss}s`;}

function badge(status){
  if(status==='ready')return '<span class="badge badge-ok">ready</span>';
  if(status==='busy')return '<span class="badge badge-busy">busy</span>';
  return '<span class="badge badge-off">'+status+'</span>';
}

function renderConn(c){
  return `<div class="kv"><span class="k">Broker</span><span class="v">${c.broker}</span></div>
<div class="kv"><span class="k">Transport</span><span class="v">${c.transport}</span></div>
<div class="kv"><span class="k">Client ID</span><span class="v"><code>${c.client_id}</code></span></div>
<div class="kv"><span class="k">Status</span><span class="v">${c.connected?'<span style="color:var(--green)">Connected</span>':'<span style="color:var(--red)">Disconnected</span>'}</span></div>
<div class="kv"><span class="k">Uptime</span><span class="v">${fmt(c.uptime_seconds)}</span></div>`;
}

function renderCfg(c){
  return `<div class="kv"><span class="k">Timeout</span><span class="v">${c.timeout_sec}s</span></div>
<div class="kv"><span class="k">Session Expiry</span><span class="v">${c.session_expiry}s</span></div>
<div class="kv"><span class="k">Debug Mode</span><span class="v">${c.debug_mode?'ON':'OFF'}</span></div>`;
}

function renderStats(s){
  return `<div class="stat"><div class="n">${s.total_requests}</div><div class="l">Total Requests</div></div>
<div class="stat"><div class="n" style="color:var(--accent)">${s.active_streams}</div><div class="l">Active Streams</div></div>
<div class="stat"><div class="n" style="color:var(--green)">${s.completed}</div><div class="l">Completed</div></div>
<div class="stat"><div class="n" style="color:var(--red)">${s.failed}</div><div class="l">Failed</div></div>
<div class="stat"><div class="n">${s.total_chunks}</div><div class="l">Chunks Processed</div></div>
<div class="stat"><div class="n">${s.heartbeats_rx}</div><div class="l">Heartbeats RX</div></div>
<div class="stat"><div class="n">${s.pending_queues}</div><div class="l">Pending Queues</div></div>`;
}

function renderWorkers(w){
  if(!w.length)return '<div class="empty">No workers connected. Open a Zen Bridge browser tab.</div>';
  let h='<table><thead><tr><th>Model</th><th>Session ID</th><th>Status</th><th>Pressure</th><th>Last Seen</th></tr></thead><tbody>';
  w.forEach(r=>{
    h+=`<tr><td><strong>${r.model}</strong></td><td><code>${r.sid}</code></td>
        <td>${badge(r.status)}</td><td>${r.pressure.toFixed(1)}</td>
        <td>${r.last_seen_ago.toFixed(0)}s ago</td></tr>`;
  });
  return h+'</tbody></table>';
}

function renderLog(items){
  const f=filter==='all'?items:items.filter(i=>i.kind===filter);
  if(!f.length)return '<div class="empty">No activity</div>';
  return f.map(i=>`<div class="log-row">
    <span class="log-t">${i.time}</span>
    <span class="log-k ${i.kind}">${i.kind}</span>
    <span class="log-topic">${i.topic}</span>
    <span class="log-sum">${i.summary}</span>
  </div>`).join('');
}

function setFilter(f,btn){
  filter=f;
  document.querySelectorAll('.fbtn[data-f]').forEach(b=>b.classList.remove('act'));
  btn.classList.add('act');
  document.getElementById('logBox').innerHTML=renderLog(logData);
}
function clearLog(){logData=[];document.getElementById('logBox').innerHTML='<div class="empty">Cleared</div>';}

function buildFilters(){
  const kinds=['all','heartbeat','response','request','system'];
  document.getElementById('filters').innerHTML=kinds.map(k=>
    `<button class="fbtn${k==='all'?' act':''}" data-f="${k}" onclick="setFilter('${k}',this)">${k}</button>`
  ).join('');
}

async function refresh(){
  try{
    const r=await fetch(API+'/admin/api/stats');const d=await r.json();
    // connection
    const dot=document.getElementById('dot');
    dot.className=d.connection.connected?'dot on':'dot off';
    document.getElementById('connLabel').textContent=d.connection.connected?'Connected':'Disconnected';
    document.getElementById('connCard').innerHTML=renderConn(d.connection);
    document.getElementById('cfgCard').innerHTML=renderCfg(d.config);
    // stats
    document.getElementById('uptime').textContent='Uptime: '+fmt(d.connection.uptime_seconds);
    document.getElementById('statsGrid').innerHTML=renderStats(d.stats);
    // workers
    document.getElementById('wCount').textContent=d.workers.count+' active';
    document.getElementById('wTable').innerHTML=renderWorkers(d.workers.details);
    // log
    if(d.recent_activity.length){
      logData=d.recent_activity;
      document.getElementById('logBox').innerHTML=renderLog(logData);
    }
    // queues
    document.getElementById('qCount').textContent=d.stats.pending_queues;
    document.getElementById('qInfo').textContent=d.stats.pending_queues>0
      ? `${d.stats.pending_queues} request(s) waiting for worker response`
      : 'No pending requests';
  }catch(e){
    document.getElementById('connLabel').textContent='API Error';
    document.getElementById('dot').className='dot off';
  }
}

buildFilters();
refresh();
setInterval(refresh,2500);
</script>
</body></html>"""

# ════════════════════════════════════════════════════════════
# FASTAPI APPLICATION
# ════════════════════════════════════════════════════════════

@asynccontextmanager
async def lifespan(app: FastAPI):
    engine.set_loop(asyncio.get_running_loop())
    engine.mqtt.connect(config.BROKER_HOST, config.BROKER_PORT)
    engine.mqtt.loop_start()
    logger.info("🚀  Zen Proxy starting on %s:%s", config.API_HOST, config.API_PORT)
    yield
    engine.mqtt.loop_stop()
    engine.mqtt.disconnect()

app = FastAPI(title="Zen AI Proxy v3.0", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

# ── pages ────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def landing():
    return HTMLResponse(LANDING_HTML)

@app.get("/chat", response_class=HTMLResponse)
async def chat_page():
    return HTMLResponse(CHAT_HTML)

@app.get("/admin", response_class=HTMLResponse)
async def admin_page():
    return HTMLResponse(ADMIN_HTML)

# ── admin stats API ──────────────────────────────────────────

@app.get("/admin/api/stats")
async def admin_stats():
    active = engine.get_active_workers()
    now = time.time()
    return {
        "connection": {
            "connected": engine.connected,
            "broker": f"{config.BROKER_HOST}:{config.BROKER_PORT}",
            "transport": config.WS_TRANSPORT,
            "client_id": engine.client_id,
            "uptime_seconds": int(now - engine.stats["start_time"]),
        },
        "workers": {
            "count": len(active),
            "details": [
                dict(sid=sid, model=i["model"], status=i["status"],
                     pressure=i["pressure"],
                     last_seen_ago=round(now - i["last_seen"], 1))
                for sid, i in active.items()
            ],
        },
        "stats": {
            "total_requests": engine.stats["total_requests"],
            "active_streams": engine.stats["active_streams"],
            "completed": engine.stats["completed"],
            "failed": engine.stats["failed"],
            "total_chunks": engine.stats["total_chunks"],
            "heartbeats_rx": engine.stats["heartbeats_rx"],
            "pending_queues": len(engine._queues),
        },
        "recent_activity": list(reversed(list(engine.activity_log)))[:100],
        "config": {
            "timeout_sec": config.TIMEOUT_SEC,
            "session_expiry": config.SESSION_EXPIRY,
            "debug_mode": config.DEBUG_MODE,
        },
    }

# ── OpenAI-compatible API ────────────────────────────────────

@app.get("/v1/models")
async def list_models():
    active = engine.get_active_workers()
    data = [{"id": "auto", "object": "model", "owned_by": "zen", "created": 1700000000}]
    for sid, info in active.items():
        data.append({
            "id": f"{info['model']}:{sid}",
            "object": "model",
            "owned_by": "zen",
            "created": 1700000000,
        })
    return {"object": "list", "data": data}

@app.options("/v1/chat/completions")
async def options_chat_completions():
    return Response(
        status_code=200,
        headers={
            "Allow": "POST, OPTIONS",
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Methods": "POST, OPTIONS",
            "Access-Control-Allow-Headers": "Content-Type, Authorization",
        },
    )


@app.post("/v1/chat/completions")
async def chat_completions(req: ChatCompletionRequest, request: Request):
    chat_id = f"chatcmpl-{uuid.uuid4().hex[:12]}"
    created  = int(time.time())

    if req.stream:
        async def generate():
            try:
                async for chunk in engine.chat(req):
                    if await request.is_disconnected():
                        break
                    choices = chunk.get("choices", [])
                    if not choices:
                        continue
                    raw_delta = choices[0].get("delta") or choices[0].get("message") or {}
                    resp = ChatCompletionChunk(
                        id=chat_id, created=created, model=req.model,
                        choices=[ChoiceChunk(
                            delta=ChoiceDelta(
                                content=raw_delta.get("content"),
                                reasoning_content=raw_delta.get("reasoning_content"),
                            ),
                            finish_reason=choices[0].get("finish_reason"),
                        )],
                    )
                    yield f"data: {resp.model_dump_json(exclude_none=True)}\n\n"
                if not await request.is_disconnected():
                    yield "data: [DONE]\n\n"
            except HTTPException as he:
                yield f"data: {json.dumps({'error': he.detail})}\n\n"
            except Exception as exc:
                logger.error("Stream error: %s", exc)
                yield f"data: {json.dumps({'error': str(exc)})}\n\n"

        return StreamingResponse(generate(), media_type="text/event-stream")

    # non-streaming
    full_content = ""
    full_reasoning = ""
    async for chunk in engine.chat(req):
        choices = chunk.get("choices", [])
        if not choices:
            continue
        delta = choices[0].get("delta") or choices[0].get("message") or {}
        full_content   += delta.get("content", "") or ""
        full_reasoning += delta.get("reasoning_content", "") or ""

    return {
        "id": chat_id,
        "object": "chat.completion",
        "created": created,
        "model": req.model,
        "choices": [{
            "message": {
                "role": "assistant",
                "content": full_content,
                **({"reasoning_content": full_reasoning} if full_reasoning else {}),
            },
            "finish_reason": "stop",
            "index": 0,
        }],
    }

# ════════════════════════════════════════════════════════════

if __name__ == "__main__":
    uvicorn.run(app, host=config.API_HOST, port=config.API_PORT)