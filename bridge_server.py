#!/usr/bin/env python3
"""Arena Termux Bridge — zero-dependency (Python stdlib only).
Runs in Termux on Android. Exposes an OpenAI-compatible API on :9080.
The actual Arena chat happens in your logged-in Firefox tab (arena_bridge.user.js);
this server just passes questions to that tab and answers back.
Env: PORT=9080 BRIDGE_KEY=123456
"""
import json, os, time, uuid, threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

PORT = int(os.environ.get("PORT", "9080"))
KEY = os.environ.get("BRIDGE_KEY", "123456")
HERE = os.path.dirname(os.path.abspath(__file__))

lock = threading.Lock()
pending = []            # [{id, prompt, messages, event, answer, error, raw}]
by_id = {}
logs = []
state = {"browser_seen": 0, "models": ["arena-current-model"], "busy": False, "last_debug": ""}

def log(msg):
    line = time.strftime("%H:%M:%S ") + str(msg)
    print(line, flush=True)
    with lock:
        logs.append(line); del logs[:-60]

def auth_ok(h):
    return h.headers.get("Authorization", "") == f"Bearer {KEY}" or h.headers.get("X-Bridge-Key", "") == KEY

class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def _send(self, code, body, ctype="application/json"):
        data = body if isinstance(body, (bytes, str)) else json.dumps(body)
        if isinstance(data, str): data = data.encode()
        self.send_response(code); self.send_header("Content-Type", ctype)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization, X-Bridge-Key")
        self.send_header("Content-Length", str(len(data))); self.end_headers(); self.wfile.write(data)
    def _json(self):
        n = int(self.headers.get("Content-Length", 0) or 0)
        try: return json.loads(self.rfile.read(n) or b"{}")
        except Exception: return {}
    def do_OPTIONS(self): self._send(204, b"")
    def do_GET(self):
        p = urlparse(self.path).path
        if p == "/health":
            return self._send(200, {"ok": True, "browserConnected": time.time() - state["browser_seen"] < 35,
                                    "busy": state["busy"], "pending": len(pending), "models": state["models"]})
        if p == "/v1/models":
            if not auth_ok(self): return self._send(401, {"error": "bad key"})
            return self._send(200, {"object": "list", "data": [{"id": m, "object": "model"} for m in state["models"]]})
        if p == "/arena_bridge.user.js":
            try: return self._send(200, open(os.path.join(HERE, "arena_bridge.user.js"), "rb").read(), "text/javascript")
            except Exception as e: return self._send(404, {"error": str(e)})
        if p.startswith("/v1/jobs/"):
            if not auth_ok(self): return self._send(401, {"error": "bad key"})
            job = by_id.get(p.split("/")[-1])
            if not job: return self._send(404, {"error": "job not found (server restarted?)"})
            st = "done" if job["event"].is_set() and not job["error"] else "error" if job["event"].is_set() else ("answering" if state["busy"] else "queued")
            return self._send(200, {"id": job["id"], "status": st, "answer": job["answer"], "error": job["error"]})
        if p == "/debug":
            return self._send(200, {"browserConnected": time.time() - state["browser_seen"] < 35, "logs": logs[-30:], "lastRawSample": state["last_debug"][-8000:]})
        if p == "/":
            return self._send(200, DASHBOARD, "text/html")
        return self._send(404, {"error": "not found"})

    def do_POST(self):
        p = urlparse(self.path).path
        if p == "/bridge/poll":  # browser runner long-poll
            if self.headers.get("X-Bridge-Key", "") != KEY: return self._send(401, {"error": "bad key"})
            body = self._json()
            with lock:
                state["browser_seen"] = time.time()
                if body.get("models"): state["models"] = body["models"][:50]
            deadline = time.time() + 25
            while time.time() < deadline:
                with lock:
                    state["browser_seen"] = time.time()
                    if pending:
                        job = pending.pop(0); state["busy"] = True
                        return self._send(200, {"requestId": job["id"], "prompt": job["prompt"]})
                time.sleep(0.5)
            return self._send(200, {"empty": True})
        if p == "/bridge/result":  # browser runner posts answer
            if self.headers.get("X-Bridge-Key", "") != KEY: return self._send(401, {"error": "bad key"})
            body = self._json()
            job = by_id.get(body.get("requestId"))
            with lock: state["browser_seen"] = time.time(); state["busy"] = False
            if body.get("rawSample"): state["last_debug"] = str(body["rawSample"])
            log(f"result {body.get('requestId')}: ok={body.get('ok')} len={len(body.get('answer') or '')} err={body.get('error')}")
            if job:
                job["answer"] = body.get("answer") or ""; job["error"] = body.get("error") or ""
                job["event"].set()
            return self._send(200, {"ok": True})
        if p == "/v1/jobs":  # dashboard: submit now, poll GET /v1/jobs/<id> (survives tab switching)
            if not auth_ok(self): return self._send(401, {"error": {"message": "bad key"}})
            body = self._json()
            msgs = body.get("messages") or []
            user = [m.get("content", "") for m in msgs if m.get("role") == "user"]
            if not user: return self._send(400, {"error": {"message": "no user message"}})
            prompt = user[-1] if isinstance(user[-1], str) else json.dumps(user[-1])
            job = {"id": uuid.uuid4().hex[:10], "prompt": prompt, "event": threading.Event(), "answer": "", "error": ""}
            with lock: pending.append(job); by_id[job["id"]] = job
            log(f"question queued {job['id']}: {prompt[:80]!r} (browser={'online' if time.time()-state['browser_seen']<35 else 'OFFLINE'})")
            return self._send(202, {"id": job["id"], "status": "queued"})
        if p == "/v1/chat/completions":
            if not auth_ok(self): return self._send(401, {"error": {"message": "bad key"}})
            body = self._json()
            msgs = body.get("messages") or []
            user = [m.get("content", "") for m in msgs if m.get("role") == "user"]
            if not user: return self._send(400, {"error": {"message": "no user message"}})
            # Send only the latest question; the Arena tab keeps the conversation context.
            prompt = user[-1] if isinstance(user[-1], str) else json.dumps(user[-1])
            job = {"id": uuid.uuid4().hex[:10], "prompt": prompt, "event": threading.Event(), "answer": "", "error": ""}
            with lock: pending.append(job); by_id[job["id"]] = job
            log(f"question queued {job['id']}: {prompt[:80]!r} (browser={'online' if time.time()-state['browser_seen']<35 else 'OFFLINE'})")
            if not job["event"].wait(240):
                return self._send(504, {"error": {"message": "Arena tab did not answer in 240s. Is Firefox open, logged in, bridge showing CONNECTED?"}})
            if job["error"] and not job["answer"]:
                return self._send(502, {"error": {"message": job["error"]}})
            return self._send(200, {"id": "chatcmpl-" + job["id"], "object": "chat.completion",
                "model": body.get("model") or state["models"][0],
                "choices": [{"index": 0, "message": {"role": "assistant", "content": job["answer"]}, "finish_reason": "stop"}]})
        return self._send(404, {"error": "not found"})

DASHBOARD = """<!doctype html><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">
<title>Arena Termux Bridge</title><body style="font-family:system-ui;background:#0f1225;color:#eef;padding:18px;max-width:720px;margin:auto">
<h2>🌉 Arena Termux Bridge</h2><p id=st>checking…</p>
<p>1) Firefox tab with Arena must show <b>CONNECTED</b> &nbsp; 2) Test here &nbsp; 3) Send admin the tunnel link</p>
<textarea id=q rows=3 style="width:100%;padding:10px;border-radius:8px">Explain photosynthesis in 3 lines</textarea>
<button id=b style="padding:12px 18px;border:0;border-radius:8px;background:#5b6cff;color:#fff;font-weight:700;width:100%;margin-top:8px">Ask Arena ▶</button>
<pre id=out style="white-space:pre-wrap;background:#1c2148;padding:12px;border-radius:8px;min-height:80px"></pre>
<p><a href="/debug" style="color:#8ab4ff">Debug logs (send this to admin if answers look wrong)</a> ·
<a href="/arena_bridge.user.js" style="color:#8ab4ff">Install / update browser script</a></p>
<script>
async function st(){try{let j=await(await fetch('/health')).json();document.getElementById('st').innerHTML=j.browserConnected?'🟢 Arena browser tab: <b>CONNECTED</b>':'🔴 Arena browser tab: <b>NOT connected</b> — open arena.ai in Firefox, login, keep the tab open'}catch(e){document.getElementById('st').textContent='server error '+e}}
st();setInterval(st,5000);
document.getElementById('b').onclick=async()=>{let o=document.getElementById('out');o.textContent='⏳ Submitting…';
 try{let r=await fetch('/v1/jobs',{method:'POST',headers:{'Content-Type':'application/json',Authorization:'Bearer 123456'},body:JSON.stringify({messages:[{role:'user',content:document.getElementById('q').value}]})});
 let j=await r.json();if(!j.id)throw new Error(JSON.stringify(j));localStorage.setItem('job',j.id);poll(j.id)}catch(e){o.textContent='ERROR submitting: '+e+' — is the Termux bridge still running?'}};
async function poll(id){let o=document.getElementById('out');o.textContent='⏳ Question sent to Arena tab. You can switch to the Arena tab now — keep it on screen. This page will fetch the answer when ready…';
 while(true){await new Promise(r=>setTimeout(r,3000));try{let j=await(await fetch('/v1/jobs/'+id,{headers:{Authorization:'Bearer 123456'}})).json();
  if(j.status==='done'){o.textContent=j.answer;localStorage.removeItem('job');return}
  if(j.status==='error'){o.textContent='ERROR from Arena tab: '+j.error;localStorage.removeItem('job');return}
  o.textContent='⏳ Status: '+j.status+'… switch to the Arena tab and keep it on screen.'}catch(e){o.textContent='⏳ Lost contact with bridge server, retrying… (is Termux still running?)'}}}
let old=localStorage.getItem('job');if(old)poll(old);
</script>"""

if __name__ == "__main__":
    log(f"Arena Termux Bridge on http://0.0.0.0:{PORT}  (key={KEY})")
    log("Dashboard: open http://localhost:%d in Firefox" % PORT)
    ThreadingHTTPServer(("0.0.0.0", PORT), H).serve_forever()
