"""`roundtable ui` — zero-dependency local web viewer for runs.

Serves a single embedded HTML page plus two JSON endpoints on 127.0.0.1:
    /                   the viewer page (inline CSS/JS, works offline)
    /api/runs           all runs under <cwd>/.roundtable/runs, newest first
    /api/runs/<run-id>  one run: meta + structured messages + final result

Running runs are re-polled by the page every 2 seconds.
"""
from __future__ import annotations

import json
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

RUN_ID_RE = re.compile(r"^[0-9]{8}-[0-9]{6}-[0-9a-f]{6}$")


def runs_dir(base: str) -> Path:
    return Path(base) / ".roundtable" / "runs"


def scan_runs(base: str) -> list[dict]:
    out = []
    root = runs_dir(base)
    if not root.is_dir():
        return out
    for run in sorted(root.iterdir(), reverse=True):
        meta_file = run / "meta.json"
        if not meta_file.is_file():
            continue
        try:
            meta = json.loads(meta_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        out.append({
            "run_id": meta.get("run_id", run.name),
            "started": meta.get("started"),
            "status": meta.get("status", "unknown"),
            "mode": meta.get("mode"),
            "lead": meta.get("lead"),
            "task": (meta.get("task") or "")[:200],
            "rounds": len(meta.get("verdicts", [])),
        })
    return out


def load_run(base: str, run_id: str) -> dict | None:
    if not RUN_ID_RE.match(run_id):
        return None
    run = runs_dir(base) / run_id
    meta_file = run / "meta.json"
    if not meta_file.is_file():
        return None
    try:
        meta = json.loads(meta_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None

    messages = []
    jsonl = run / "messages.jsonl"
    if jsonl.is_file():
        for line in jsonl.read_text(encoding="utf-8").splitlines():
            try:
                messages.append(json.loads(line))
            except json.JSONDecodeError:
                pass

    transcript = ""
    if not messages:  # pre-v0.2 run: fall back to the raw markdown transcript
        try:
            transcript = (run / "transcript.md").read_text(encoding="utf-8")
        except OSError:
            pass

    result = ""
    try:
        result = (run / "result.md").read_text(encoding="utf-8")
    except OSError:
        pass

    return {"meta": meta, "messages": messages, "transcript": transcript, "result": result}


class _Handler(BaseHTTPRequestHandler):
    base = "."  # overridden by serve()

    def log_message(self, fmt, *args):  # silence per-request stderr noise
        pass

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code: int, obj) -> None:
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8")

    def do_GET(self):  # noqa: N802 (stdlib naming)
        path = self.path.split("?", 1)[0]
        if path == "/":
            self._send(200, PAGE.encode("utf-8"), "text/html; charset=utf-8")
        elif path == "/api/runs":
            self._json(200, scan_runs(self.base))
        elif path.startswith("/api/runs/"):
            run = load_run(self.base, path[len("/api/runs/"):])
            if run is None:
                self._json(404, {"error": "run not found"})
            else:
                self._json(200, run)
        else:
            self._json(404, {"error": "not found"})


def serve(base: str, port: int = 8642) -> None:
    handler = type("Handler", (_Handler,), {"base": base})
    server = ThreadingHTTPServer(("127.0.0.1", port), handler)
    print(f"roundtable ui: watching {runs_dir(base)}")
    print(f"open http://127.0.0.1:{server.server_address[1]}  (Ctrl-C to stop)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Roundtable</title>
<style>
:root{--bg:#0f1217;--panel:#171c24;--panel2:#1e2530;--text:#dde3ec;--dim:#8b95a7;
--accent:#5b9dd9;--green:#3fb96f;--orange:#e0913f;--red:#d95b5b;--border:#2a3342}
*{box-sizing:border-box}
body{margin:0;font:14px/1.55 system-ui,'Segoe UI',sans-serif;background:var(--bg);color:var(--text);display:flex;height:100vh}
#side{width:300px;min-width:220px;border-right:1px solid var(--border);overflow-y:auto;background:var(--panel)}
#side h1{font-size:16px;padding:14px 16px;margin:0;border-bottom:1px solid var(--border)}
#side h1 small{color:var(--dim);font-weight:normal}
.run{padding:10px 14px;border-bottom:1px solid var(--border);cursor:pointer}
.run:hover,.run.sel{background:var(--panel2)}
.run .task{white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.run .sub{color:var(--dim);font-size:12px;margin-top:2px}
#main{flex:1;overflow-y:auto;padding:20px 26px}
.badge{display:inline-block;padding:1px 8px;border-radius:9px;font-size:11px;font-weight:600;vertical-align:1px}
.b-running{background:#274; background:var(--accent);color:#fff}
.b-approved{background:var(--green);color:#fff}
.b-max_rounds{background:var(--orange);color:#fff}
.b-error,.b-interrupted{background:var(--red);color:#fff}
.b-unknown{background:var(--dim);color:#fff}
.b-APPROVE{background:var(--green);color:#fff}
.b-REVISE{background:var(--orange);color:#fff}
.b-score{background:var(--panel2);border:1px solid var(--border);color:var(--text)}
.card{background:var(--panel);border:1px solid var(--border);border-radius:10px;padding:14px 16px;margin-bottom:16px}
.card h2{margin:0 0 8px;font-size:15px}
.msg{max-width:78%;margin-bottom:14px}
.msg .who{font-size:12px;color:var(--dim);margin-bottom:3px}
.msg .bubble{border:1px solid var(--border);border-radius:12px;padding:10px 14px;background:var(--panel)}
.msg.leader{margin-right:auto}
.msg.leader .bubble{border-top-left-radius:3px}
.msg.reviewer{margin-left:auto;text-align:left}
.msg.reviewer .who{text-align:right}
.msg.reviewer .bubble{background:var(--panel2);border-top-right-radius:3px}
.bubble pre{background:#0b0e13;border:1px solid var(--border);border-radius:6px;padding:8px 10px;overflow-x:auto;font-size:12.5px}
.bubble code{background:#0b0e13;border-radius:4px;padding:1px 4px;font-size:12.5px}
.bubble h1,.bubble h2,.bubble h3{font-size:14px;margin:10px 0 4px}
.bubble p{margin:6px 0}
#empty{color:var(--dim);margin-top:40vh;text-align:center}
.dim{color:var(--dim)}
</style>
</head>
<body>
<div id="side"><h1>Roundtable <small id="count"></small></h1><div id="runs"></div></div>
<div id="main"><div id="empty">select a run on the left</div></div>
<script>
"use strict";
let selected=null,timer=null;

const esc=s=>s.replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;");
function md(src){                       // tiny offline markdown-lite renderer
  const parts=esc(src).split(/```(?:[^\\n]*)\\n?/);
  let out="";
  parts.forEach((p,i)=>{
    if(i%2)return out+="<pre>"+p+"</pre>";
    let h=p
      .replace(/^######? (.+)$/gm,"<h3>$1</h3>")
      .replace(/^#{1,4} (.+)$/gm,"<h2>$1</h2>")
      .replace(/\\*\\*([^*]+)\\*\\*/g,"<b>$1</b>")
      .replace(/`([^`]+)`/g,"<code>$1</code>");
    out+="<p>"+h.trim().replace(/\\n{2,}/g,"</p><p>").replace(/\\n/g,"<br>")+"</p>";
  });
  return out;
}
const badge=(cls,txt)=>`<span class="badge b-${cls}">${esc(String(txt))}</span>`;

async function loadRuns(){
  const runs=await (await fetch("/api/runs")).json();
  document.getElementById("count").textContent=runs.length+" runs";
  document.getElementById("runs").innerHTML=runs.map(r=>
    `<div class="run ${r.run_id===selected?"sel":""}" onclick="pick('${r.run_id}')">
      <div class="task">${esc(r.task||"(no task)")}</div>
      <div class="sub">${badge(r.status,r.status)} ${esc(r.mode||"")} · lead ${esc(r.lead||"")} · R${r.rounds} · ${esc(r.started||"")}</div>
    </div>`).join("");
}

function verdictFor(meta,round){
  return (meta.verdicts||[]).find(v=>v.round===round);
}

async function show(){
  if(!selected)return;
  const resp=await fetch("/api/runs/"+selected);
  if(!resp.ok)return;
  const run=await resp.json(),m=run.meta;
  let html=`<div class="card"><h2>${badge(m.status,m.status)} ${esc(m.mode)} · lead: ${esc(m.lead)}
    · ${(m.verdicts||[]).length} round(s) <span class="dim">· ${esc(m.run_id)}</span></h2>
    <div>${esc(m.task||"")}</div></div>`;
  if(run.messages.length){
    html+=run.messages.map(msg=>{
      let badges="";
      if(msg.role==="reviewer"){
        const v=verdictFor(m,msg.round)||{};
        if(v.score!=null)badges+=" "+badge("score","SCORE "+v.score);
        if(v.verdict)badges+=" "+badge(v.verdict,v.verdict);
      }
      return `<div class="msg ${msg.role}">
        <div class="who">R${msg.round} · <b>${esc(msg.speaker)}</b> (${esc(msg.role)})
          <span class="dim">${msg.duration_s}s</span>${badges}</div>
        <div class="bubble">${md(msg.text)}</div></div>`;
    }).join("");
  }else if(run.transcript){
    html+=`<div class="card"><h2>transcript (pre-v0.2 run)</h2>${md(run.transcript)}</div>`;
  }
  if(run.result.trim())
    html+=`<div class="card"><h2>final result</h2>${md(run.result)}</div>`;
  if((m.warnings||[]).length)
    html+=`<div class="card"><h2>warnings</h2><div class="dim">${m.warnings.map(esc).join("<br>")}</div></div>`;
  const el=document.getElementById("main");
  const stick=el.scrollTop+el.clientHeight>=el.scrollHeight-60;
  el.innerHTML=html;
  if(m.status==="running"&&stick)el.scrollTop=el.scrollHeight;
  clearInterval(timer);
  if(m.status==="running")timer=setInterval(()=>{show();loadRuns();},2000);
}

function pick(id){selected=id;loadRuns();show();}
loadRuns();setInterval(loadRuns,5000);
</script>
</body>
</html>
"""
