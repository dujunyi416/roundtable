"""`roundtable ui` — zero-dependency local web viewer for runs.

Serves a single embedded HTML page plus two JSON endpoints on 127.0.0.1:
    /                   the viewer page (inline CSS/JS, works offline)
    /api/runs           all runs under <cwd>/.roundtable/runs, newest first
    /api/runs/<run-id>  one run: meta + structured messages + final result

Running runs are re-polled by the page every 2 seconds.
"""
from __future__ import annotations

import json
import hmac
import re
import secrets
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .control import RequestError, RunController
from .models import discover_models
from .project import ProjectCatalog, ProjectError, ProjectRoom

RUN_ID_RE = re.compile(r"^[0-9]{8}-[0-9]{6}-[0-9a-f]{6}$")


def runs_dir(base: str) -> Path:
    return Path(base) / ".roundtable" / "runs"


def scan_runs(base: str) -> list[dict]:
    out = []
    root = runs_dir(base)
    if not root.is_dir():
        return out
    try:
        default = ProjectCatalog(base).get(None)
        default_project = {
            key: default[key] for key in ("id", "name", "project_path", "git_path")
        }
    except ProjectError:
        default_project = None
    for run in sorted(root.iterdir(), reverse=True):
        if not RUN_ID_RE.fullmatch(run.name):
            continue
        meta_file = run / "meta.json"
        if not meta_file.is_file():
            continue
        try:
            meta = json.loads(meta_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        out.append({
            "run_id": run.name,
            "started": meta.get("started"),
            "status": meta.get("status", "unknown"),
            "mode": meta.get("mode"),
            "lead": meta.get("lead"),
            "task": (meta.get("task") or "")[:200],
            "rounds": len(meta.get("verdicts", [])),
            "next_action": meta.get("next_action"),
            "project": meta.get("config", {}).get("project") or default_project,
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

    meta["run_id"] = run_id
    result = ""
    try:
        result = (run / "result.md").read_text(encoding="utf-8")
    except OSError:
        pass

    plan = ""
    try:
        plan = (run / "plan.md").read_text(encoding="utf-8")
    except OSError:
        pass

    return {
        "meta": meta, "messages": messages, "transcript": transcript,
        "result": result, "plan": plan,
    }


def choose_directory(initial_path: str) -> str | None:
    """Open the operating system's folder picker and return an absolute path."""
    try:
        import tkinter as tk
        from tkinter import filedialog
    except ImportError as exc:
        raise ProjectError("folder selection is unavailable on this Python installation") from exc
    initial = Path(initial_path).expanduser()
    if not initial.is_dir():
        initial = Path.home()
    root = None
    try:
        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        selected = filedialog.askdirectory(
            parent=root, initialdir=str(initial), title="选择项目文件夹",
        )
    except tk.TclError as exc:
        raise ProjectError(f"cannot open folder selector: {exc}") from exc
    finally:
        if root is not None:
            root.destroy()
    return str(Path(selected).resolve()) if selected else None


class _Handler(BaseHTTPRequestHandler):
    base = "."  # overridden by serve()
    controller: RunController | None = None
    auth_token = ""
    csp_nonce = ""

    def log_message(self, fmt, *args):  # silence per-request stderr noise
        pass

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'none'; connect-src 'self'; img-src 'self' data:; "
            f"style-src 'nonce-{self.csp_nonce}'; script-src 'nonce-{self.csp_nonce}'; "
            "base-uri 'none'; frame-ancestors 'none'; form-action 'none'",
        )
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code: int, obj) -> None:
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8")

    def _body_json(self) -> dict:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            raise RequestError("invalid Content-Length") from exc
        if length <= 0 or length > 1_000_000:
            raise RequestError("JSON body is required and must be under 1 MB")
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RequestError("invalid JSON body") from exc

    def _host_allowed(self) -> bool:
        host = self.headers.get("Host", "")
        port = self.server.server_address[1]
        return host.lower() in {f"127.0.0.1:{port}", f"localhost:{port}"}

    def _origin_allowed(self) -> bool:
        origin = self.headers.get("Origin", "").lower()
        port = self.server.server_address[1]
        return origin in {f"http://127.0.0.1:{port}", f"http://localhost:{port}"}

    def _guard_host(self) -> bool:
        if self._host_allowed():
            return True
        self._json(403, {"error": "forbidden Host header"})
        return False

    def _guard_write(self) -> bool:
        content_type = self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
        supplied = self.headers.get("X-Roundtable-Token", "")
        if content_type != "application/json":
            self._json(415, {"error": "Content-Type must be application/json"})
            return False
        if not self._origin_allowed():
            self._json(403, {"error": "forbidden Origin header"})
            return False
        if not self.auth_token or not hmac.compare_digest(supplied, self.auth_token):
            self._json(403, {"error": "invalid Roundtable token"})
            return False
        return True

    def do_GET(self):  # noqa: N802 (stdlib naming)
        if not self._guard_host():
            return
        path = self.path.split("?", 1)[0]
        if path == "/":
            page = PAGE.replace("__ROUNDTABLE_TOKEN__", json.dumps(self.auth_token))
            page = page.replace("__CSP_NONCE__", self.csp_nonce)
            self._send(200, page.encode("utf-8"), "text/html; charset=utf-8")
        elif path == "/api/runs":
            self._json(200, scan_runs(self.base))
        elif path == "/api/models":
            self._json(200, discover_models())
        elif path == "/api/project":
            try:
                self._json(200, ProjectRoom(self.base).load())
            except ProjectError as exc:
                self._json(500, {"error": str(exc)})
        elif path == "/api/projects":
            try:
                self._json(200, ProjectCatalog(self.base).load())
            except ProjectError as exc:
                self._json(500, {"error": str(exc)})
        elif path.startswith("/api/runs/"):
            run = load_run(self.base, path[len("/api/runs/"):])
            if run is None:
                self._json(404, {"error": "run not found"})
            else:
                self._json(200, run)
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self):  # noqa: N802 (stdlib naming)
        if not self._guard_host() or not self._guard_write():
            return
        path = self.path.split("?", 1)[0]
        if self.controller is None:
            self._json(503, {"error": "run control is unavailable"})
            return
        try:
            body = self._body_json()
            if path == "/api/project":
                self._json(200, ProjectRoom(self.base).save(body))
                return
            if path == "/api/projects":
                self._json(200, ProjectCatalog(self.base).save(body))
                return
            if path == "/api/folders/select":
                initial = body.get("initial_path", self.base)
                if not isinstance(initial, str):
                    raise RequestError("initial_path must be text")
                self._json(200, {"path": choose_directory(initial)})
                return
            if path == "/api/runs":
                self._json(201, self.controller.start(body))
                return
            match = re.fullmatch(r"/api/runs/([^/]+)/control", path)
            if match and RUN_ID_RE.match(match.group(1)):
                self._json(200, self.controller.command(match.group(1), body))
                return
            match = re.fullmatch(r"/api/runs/([^/]+)/follow-up", path)
            if match and RUN_ID_RE.match(match.group(1)):
                self._json(201, self.controller.follow_up(match.group(1), body))
                return
        except (RequestError, ProjectError) as exc:
            self._json(400, {"error": str(exc)})
            return
        self._json(404, {"error": "not found"})

    def do_OPTIONS(self):  # noqa: N802 (stdlib naming)
        if not self._guard_host():
            return
        if not self._origin_allowed():
            self._json(403, {"error": "forbidden Origin header"})
            return
        self._send(204, b"", "text/plain; charset=utf-8")


def serve(base: str, port: int = 8642) -> None:
    handler = type("Handler", (_Handler,), {
        "base": base,
        "controller": RunController(base),
        "auth_token": secrets.token_urlsafe(32),
        "csp_nonce": secrets.token_urlsafe(24),
    })
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
<style nonce="__CSP_NONCE__">
:root{--bg:#0f1217;--panel:#171c24;--panel2:#1e2530;--text:#dde3ec;--dim:#8b95a7;
--accent:#5b9dd9;--green:#3fb96f;--orange:#e0913f;--red:#d95b5b;--border:#2a3342}
*{box-sizing:border-box}
body{margin:0;font:14px/1.55 system-ui,'Segoe UI',sans-serif;background:var(--bg);color:var(--text);display:flex;height:100vh}
#side{width:350px;min-width:260px;border-right:1px solid var(--border);overflow-y:auto;background:var(--panel)}
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
.b-max_rounds,.b-needs_human_decision,.b-orphaned{background:var(--orange);color:#fff}
.b-error,.b-interrupted{background:var(--red);color:#fff}
.b-cancelled{background:var(--dim);color:#fff}
.b-waiting{background:#7c5ce0;color:#fff}.b-running{background:var(--accent);color:#fff}
.b-pause_requested,.b-cancelling{background:var(--orange);color:#fff}
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
.msg.human{margin:16px auto;max-width:90%}.msg.human .who{text-align:center;color:#b9a7ff}
.msg.human .bubble{background:#241f38;border-color:#604e8f;border-radius:8px}
.bubble pre{background:#0b0e13;border:1px solid var(--border);border-radius:6px;padding:8px 10px;overflow-x:auto;font-size:12.5px}
.bubble code{background:#0b0e13;border-radius:4px;padding:1px 4px;font-size:12.5px}
.bubble h1,.bubble h2,.bubble h3{font-size:14px;margin:10px 0 4px}
.bubble p{margin:6px 0}
#empty{color:var(--dim);margin-top:40vh;text-align:center}
.dim{color:var(--dim)}
.compose{padding:12px;border-bottom:1px solid var(--border);display:grid;gap:8px}
.project{padding:10px 12px;border-bottom:1px solid var(--border)}.project summary{cursor:pointer;font-weight:700}.project .fields{display:grid;gap:7px;margin-top:9px}
.compose textarea,.compose input,.compose select,.humanbox textarea{width:100%;background:#0f141b;color:var(--text);border:1px solid var(--border);border-radius:6px;padding:8px;font:inherit}
.project textarea,.project input,.project select{width:100%;background:#0f141b;color:var(--text);border:1px solid var(--border);border-radius:6px;padding:7px;font:inherit}.project textarea{min-height:58px;resize:vertical}
.compose textarea{min-height:74px;resize:vertical}.grid2{display:grid;grid-template-columns:1fr 1fr;gap:7px}
.path-picker{display:grid;grid-template-columns:minmax(0,1fr) auto;gap:7px}.path-picker button{white-space:nowrap}
.project-group{padding:6px 12px;color:var(--dim);font-size:11px;font-weight:700;letter-spacing:.04em;text-transform:uppercase;background:#131820;border-bottom:1px solid var(--border)}
.modelmeta{display:flex;align-items:center;gap:8px}.modelmeta button{padding:4px 8px}.modelmeta span{font-size:11px}
button{border:0;border-radius:6px;padding:7px 11px;background:var(--accent);color:white;font-weight:600;cursor:pointer}
button.secondary{background:var(--panel2);border:1px solid var(--border)}button.warn{background:var(--orange)}button.danger{background:var(--red)}
button:disabled{opacity:.45;cursor:default}.check{display:flex;align-items:center;gap:7px;color:var(--dim);font-size:12px}
.controls{display:flex;gap:7px;flex-wrap:wrap;margin:10px 0}.humanbox{display:grid;grid-template-columns:1fr auto;gap:8px;margin-top:10px}
.rounds{display:flex;gap:7px;flex-wrap:wrap;margin-top:10px}.roundchip{background:var(--panel2);border:1px solid var(--border);padding:5px 9px;border-radius:14px;font-size:12px}
.errorline{color:#ff8e8e;font-size:12px;min-height:18px}
.activity{display:inline-flex;align-items:center;gap:7px}.pulse{width:9px;height:9px;border-radius:50%;background:var(--green);animation:pulse 1.4s infinite}@keyframes pulse{50%{opacity:.25;transform:scale(.75)}}
.workflow{display:flex;gap:8px;flex-wrap:wrap;margin-top:10px}
@media(max-width:760px){body{display:block;height:auto}#side{width:100%;height:auto;max-height:none}#main{min-height:55vh}.msg{max-width:94%}}
</style>
</head>
<body>
<div id="side"><h1>Roundtable <small id="count"></small></h1>
  <details class="project"><summary>Project Room <small id="projectName" class="dim"></small></summary><div class="fields">
    <div class="grid2"><select id="pSelect"></select><button id="newProjectButton" class="secondary">新增项目</button></div>
    <input id="pName" placeholder="项目名称"><textarea id="pMission" placeholder="项目使命：我们为什么做这件事？"></textarea>
    <div class="path-picker"><input id="pPath" placeholder="项目路径（本地工作目录）"><button id="projectFolderButton" class="secondary">选择文件夹</button></div><input id="pGit" placeholder="Git 路径（远程地址或本地仓库）">
    <textarea id="pGoals" placeholder="当前目标（每行一项）"></textarea><textarea id="pConstraints" placeholder="约束（每行一项）"></textarea>
    <textarea id="pDecisions" placeholder="已决定事项（每行一项）"></textarea><button id="saveProjectButton" class="secondary">保存项目档案</button><div id="projectError" class="errorline"></div>
  </div></details>
  <div class="compose">
    <b>新建协作</b>
    <div class="path-picker"><select id="runProject" title="按项目选择工作目录"></select><button id="runFolderButton" class="secondary">选择文件夹</button></div>
    <textarea id="task" placeholder="描述目标、背景和交付标准…"></textarea>
    <div class="grid2"><select id="mode"><option value="discuss">讨论</option><option value="plan">规划</option><option value="build">实施</option></select><select id="style"><option value="balanced">平衡审查</option><option value="adversarial">对抗审查</option></select></div>
    <div class="grid2"><select id="lead"><option value="codex">Codex 主导</option><option value="claude">Claude 主导</option></select><select id="reviewer"><option value="codex">Codex 审查</option><option value="claude">Claude 审查</option></select></div>
    <div class="grid2"><input id="leadModel" list="leadModelOptions" placeholder="主导模型（留空使用默认）"><datalist id="leadModelOptions"></datalist><input id="reviewerModel" list="reviewerModelOptions" placeholder="审查模型（留空使用默认）"><datalist id="reviewerModelOptions"></datalist></div>
    <div class="modelmeta"><button id="loadModelsButton" class="secondary">刷新模型列表</button><span id="modelStatus" class="dim"></span></div>
    <div class="grid2"><input id="leadName" placeholder="主导者名称（可选）"><input id="reviewerName" placeholder="审查者名称（可选）"></div>
    <label class="check"><input type="checkbox" id="humanGate"> 每次 AI 发言后等待我确认</label>
    <button id="startRunButton">开始协作</button><div id="createError" class="errorline"></div>
  </div><div id="runs"></div></div>
<div id="main"><div id="empty">select a run on the left</div></div>
<script nonce="__CSP_NONCE__">
"use strict";
let selected=null,timer=null,projects=[],selectedProjectId=null;
let modelCatalog={claude:{models:[]},codex:{models:[]}};
const authToken=__ROUNDTABLE_TOKEN__;

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
const badgeClasses=new Set(["running","approved","max_rounds","needs_human_decision","error","interrupted","orphaned","cancelled","waiting","pause_requested","cancelling","unknown","APPROVE","REVISE","score"]);
function makeBadge(cls,txt){
  const span=document.createElement("span");span.className="badge b-"+(badgeClasses.has(cls)?cls:"unknown");span.textContent=String(txt);return span;
}

async function post(path,data){
  const resp=await fetch(path,{method:"POST",headers:{"Content-Type":"application/json","X-Roundtable-Token":authToken},body:JSON.stringify(data)});
  const body=await resp.json();
  if(!resp.ok)throw new Error(body.error||("HTTP "+resp.status));
  return body;
}

function updateModelOptions(role){
  const provider=document.getElementById(role).value;
  const list=document.getElementById(role+"ModelOptions");
  const models=(modelCatalog[provider]||{}).models||[];
  list.replaceChildren(...models.map(model=>{
    const option=document.createElement("option");option.value=model.value;
    option.label=model.label+(model.default?"（默认）":"")+(model.description?" — "+model.description:"");
    return option;
  }));
}

async function loadModels(){
  const status=document.getElementById("modelStatus");status.textContent="正在读取…";
  try{
    const resp=await fetch("/api/models");if(!resp.ok)throw new Error("HTTP "+resp.status);
    modelCatalog=await resp.json();updateModelOptions("lead");updateModelOptions("reviewer");
    const codex=(modelCatalog.codex||{}).models||[],claude=(modelCatalog.claude||{}).models||[];
    status.textContent=`Codex 账户模型 ${codex.length} 个；Claude CLI 别名 ${claude.length} 个`;
  }catch(e){status.textContent="模型列表读取失败："+e.message;}
}

async function startRun(){
  const err=document.getElementById("createError");err.textContent="";
  try{
    const data=await post("/api/runs",{
      task:document.getElementById("task").value,mode:document.getElementById("mode").value,
      style:document.getElementById("style").value,lead:document.getElementById("lead").value,
      reviewer:document.getElementById("reviewer").value,lead_name:document.getElementById("leadName").value,
      reviewer_name:document.getElementById("reviewerName").value,lead_model:document.getElementById("leadModel").value,
      reviewer_model:document.getElementById("reviewerModel").value,project_id:document.getElementById("runProject").value,
      human_gate:document.getElementById("humanGate").checked
    });
    selected=data.run_id;document.getElementById("task").value="";await loadRuns();await show();
  }catch(e){err.textContent=e.message;}
}

const lines=id=>document.getElementById(id).value.split(/\\n/).map(x=>x.trim()).filter(Boolean);
function renderProjectChoices(){
  const editor=document.getElementById("pSelect");
  editor.replaceChildren(...projects.map(project=>{
    const option=document.createElement("option");option.value=project.id;option.textContent=project.name;return option;
  }));
  editor.value=selectedProjectId||"";
  const runner=document.getElementById("runProject"),previous=runner.value;
  runner.replaceChildren(...projects.map(project=>{
    const group=document.createElement("optgroup");group.label=project.name;
    const local=document.createElement("option");local.value=project.id;local.textContent="项目路径 · "+project.project_path;group.append(local);
    if(project.git_path){const git=document.createElement("option");git.disabled=true;git.textContent="Git 路径 · "+project.git_path;group.append(git);}
    return group;
  }));
  runner.value=projects.some(project=>project.id===previous)?previous:(selectedProjectId||"");
}
function selectProject(id){
  const p=projects.find(project=>project.id===id);if(!p)return;
  selectedProjectId=p.id;document.getElementById("pSelect").value=p.id;
  document.getElementById("runProject").value=p.id;
  document.getElementById("pName").value=p.name||"";document.getElementById("pMission").value=p.mission||"";
  document.getElementById("pPath").value=p.project_path||"";document.getElementById("pGit").value=p.git_path||"";
  document.getElementById("pGoals").value=(p.goals||[]).join("\\n");document.getElementById("pConstraints").value=(p.constraints||[]).join("\\n");
  document.getElementById("pDecisions").value=(p.decisions||[]).join("\\n");document.getElementById("projectName").textContent="· "+p.name;
}
function newProject(){
  selectedProjectId=null;["pName","pMission","pPath","pGit","pGoals","pConstraints","pDecisions"].forEach(id=>document.getElementById(id).value="");
  document.getElementById("pSelect").value="";document.getElementById("projectName").textContent="· 新项目";
}
async function loadProjects(preferred){
  try{
    const resp=await fetch("/api/projects");if(!resp.ok)throw new Error("HTTP "+resp.status);projects=await resp.json();
    selectedProjectId=(preferred&&projects.some(p=>p.id===preferred))?preferred:(selectedProjectId&&projects.some(p=>p.id===selectedProjectId)?selectedProjectId:(projects[0]||{}).id);
    renderProjectChoices();if(selectedProjectId)selectProject(selectedProjectId);
  }catch(e){document.getElementById("projectError").textContent=e.message;}
}
async function saveProject(errorId="projectError"){
  const err=document.getElementById(errorId);err.textContent="";
  try{const saved=await post("/api/projects",{id:selectedProjectId,name:document.getElementById("pName").value,project_path:document.getElementById("pPath").value,git_path:document.getElementById("pGit").value,mission:document.getElementById("pMission").value,goals:lines("pGoals"),constraints:lines("pConstraints"),decisions:lines("pDecisions")});await loadProjects(saved.id);}
  catch(e){err.textContent=e.message;}
}
async function chooseProjectFolder(saveNow){
  const err=document.getElementById(saveNow?"createError":"projectError");err.textContent="";
  const current=projects.find(project=>project.id===selectedProjectId);
  const initial=saveNow&&current?current.project_path:document.getElementById("pPath").value;
  try{
    const picked=await post("/api/folders/select",{initial_path:initial});if(!picked.path)return;
    document.getElementById("pPath").value=picked.path;
    if(!document.getElementById("pName").value){document.getElementById("pName").value=picked.path.split(/[\\\\/]/).filter(Boolean).pop()||"新项目";}
    if(saveNow)await saveProject("createError");
  }catch(e){err.textContent=e.message;}
}

async function controlRun(action){
  try{await post(`/api/runs/${selected}/control`,{action});await show();}
  catch(e){alert(e.message);}
}

async function intervene(){
  const el=document.getElementById("humanText"),message=el.value;
  try{await post(`/api/runs/${selected}/control`,{action:"intervene",message});el.value="";await show();}
  catch(e){alert(e.message);}
}

async function loadRuns(){
  const runs=await (await fetch("/api/runs")).json();
  document.getElementById("count").textContent=runs.length+" runs";
  const groups=new Map();runs.forEach(run=>{const name=(run.project||{}).name||"未分类";if(!groups.has(name))groups.set(name,[]);groups.get(name).push(run);});
  const root=document.getElementById("runs"),nodes=[];
  groups.forEach((items,name)=>{
    const heading=document.createElement("div");heading.className="project-group";heading.textContent=name;nodes.push(heading);
    items.forEach(r=>{
      const item=document.createElement("div");item.className="run"+(r.run_id===selected?" sel":"");
      item.addEventListener("click",()=>pick(r.run_id));
      const task=document.createElement("div");task.className="task";task.textContent=r.task||"(no task)";
      const sub=document.createElement("div");sub.className="sub";sub.append(makeBadge(r.status,r.status),document.createTextNode(` ${r.mode||""} · lead ${r.lead||""} · R${r.rounds} · ${r.started||""}`));
      item.append(task,sub);nodes.push(item);
    });
  });
  root.replaceChildren(...nodes);
}

function verdictFor(meta,round){
  return (meta.verdicts||[]).find(v=>v.round===round);
}

let renderedRun=null;

function ensureRunShell(run){
  if(renderedRun===run.meta.run_id)return;
  renderedRun=run.meta.run_id;
  const main=document.getElementById("main");
  const summary=document.createElement("div");summary.className="card";summary.id="runSummary";
  ["runTitle","runTask","runParticipants","runRounds","runLive","runWorkflow"].forEach(id=>{const node=document.createElement("div");node.id=id;summary.append(node);});
  const messages=document.createElement("div");messages.id="runMessages";
  const result=document.createElement("div");result.className="card";result.id="resultCard";result.hidden=true;
  const resultTitle=document.createElement("h2");resultTitle.id="resultTitle";const resultBody=document.createElement("div");resultBody.id="resultBody";result.append(resultTitle,resultBody);
  const warnings=document.createElement("div");warnings.className="card";warnings.id="warningCard";warnings.hidden=true;
  const warningTitle=document.createElement("h2");warningTitle.textContent="warnings";const warningBody=document.createElement("div");warningBody.className="dim";warningBody.id="warningBody";warnings.append(warningTitle,warningBody);
  main.replaceChildren(summary,messages,result,warnings);
}

function updateSummary(run){
  const m=run.meta,ctl=m.control||{},participants=(m.config||{}).participants||[];
  const title=document.getElementById("runTitle");title.replaceChildren(makeBadge(m.status,m.status),document.createTextNode(` ${m.mode} · lead: ${m.lead} · ${(m.verdicts||[]).length} round(s) · ${m.run_id}`));title.className="card-title";
  document.getElementById("runTask").textContent=m.task||"";
  const participantNode=document.getElementById("runParticipants");participantNode.className="dim";participantNode.textContent=participants.map(p=>`${p.name} [${p.provider}]`).join(" ↔ ");
  const rounds=document.getElementById("runRounds");rounds.className="rounds";rounds.replaceChildren(...(m.verdicts||[]).map(v=>{const chip=document.createElement("span");chip.className="roundchip";chip.textContent=`R${v.round} · ${v.score==null?"—":v.score+"/10"} · ${v.verdict||"NO VERDICT"}`;return chip;}));
  updateLiveControls(m,ctl);
  updateWorkflow(m,participants);
}

function updateLiveControls(m,ctl){
  const live=document.getElementById("runLive");
  if(m.status!=="running"){live.replaceChildren();return;}
  if(!document.getElementById("humanText")){
    const state=document.createElement("div");state.className="controls";state.id="liveState";
    const activity=document.createElement("span");activity.className="activity";const pulse=document.createElement("span");pulse.className="pulse";const activityText=document.createElement("span");activityText.id="activityText";activity.append(pulse,activityText);state.append(activity);
    [["暂停","pause","secondary"],["继续","resume","secondary"],["取消","cancel","danger"]].forEach(([label,action,cls])=>{const button=document.createElement("button");button.textContent=label;button.className=cls;button.addEventListener("click",()=>controlRun(action));state.append(button);});
    const box=document.createElement("div");box.className="humanbox";const input=document.createElement("textarea");input.id="humanText";input.placeholder="插入补充、纠正方向，或要求继续修改…";const send=document.createElement("button");send.textContent="发送并继续";send.addEventListener("click",intervene);box.append(input,send);live.append(state,box);
  }
  const heartbeat=ctl.heartbeat?new Date(ctl.heartbeat.replace(" ","T")):null;
  const age=heartbeat?Math.max(0,Math.floor((Date.now()-heartbeat.getTime())/1000)):0;
  document.getElementById("activityText").textContent=`${ctl.state||"running"} · ${ctl.phase||"agent working"}${ctl.round!=null?" · R"+ctl.round:""} · heartbeat ${age}s ago`;
}

function updateWorkflow(m,participants){
  const flow=document.getElementById("runWorkflow");flow.className="workflow";flow.replaceChildren();
  const next=m.next_action||{};
  if(next.child_run_id){const open=document.createElement("button");open.className="secondary";open.textContent="打开后续运行";open.addEventListener("click",()=>pick(next.child_run_id));flow.append(open);return;}
  if(m.status==="running")return;
  if(m.mode==="discuss"){
    const button=document.createElement("button");button.textContent="生成实施计划";button.addEventListener("click",()=>followUp("plan",m,participants));flow.append(button);
  }else if(m.mode==="plan"){
    const button=document.createElement("button");button.textContent=m.status==="approved"?"确认执行此计划":"人工确认并执行";button.addEventListener("click",()=>followUp("build",m,participants));flow.append(button);
  }
  const parent=(m.config||{}).parent_run_id;if(parent){const open=document.createElement("button");open.className="secondary";open.textContent="查看上游运行";open.addEventListener("click",()=>pick(parent));flow.append(open);}
}

async function followUp(mode,m,participants){
  try{
    if(mode==="plan"){
      if(m.status!=="approved"&&!confirm("该讨论未获 reviewer 批准。确认仍以最终综合为依据生成计划吗？"))return;
      const child=await post(`/api/runs/${m.run_id}/follow-up`,{mode:"plan",confirmed:true});selected=child.run_id;renderedRun=null;await loadRuns();await show();return;
    }
    const scope=prompt("请输入本次明确批准的执行范围：","执行计划中提出的第一批任务");if(!scope)return;
    const lead=document.getElementById("lead").value,reviewer=document.getElementById("reviewer").value;
    if(!confirm(`确认由 ${lead} 执行、${reviewer} 审查，并且本次只执行：${scope}`))return;
    const child=await post(`/api/runs/${m.run_id}/follow-up`,{mode:"build",confirmed:true,accept_unapproved:m.status!=="approved",scope,lead,reviewer});selected=child.run_id;renderedRun=null;await loadRuns();await show();
  }catch(e){alert(e.message);}
}

function renderMessages(run){
  const root=document.getElementById("runMessages"),m=run.meta,nodes=[];
  if(run.messages.length){
    run.messages.forEach(msg=>{
      const role=["leader","reviewer","human"].includes(msg.role)?msg.role:"unknown";
      const item=document.createElement("div");item.className="msg "+role;
      const who=document.createElement("div");who.className="who";who.append(document.createTextNode(`R${msg.round} · `));const name=document.createElement("b");name.textContent=msg.speaker;who.append(name,document.createTextNode(` (${role}) ${msg.duration_s}s`));
      if(role==="reviewer"){const v=verdictFor(m,msg.round)||{};if(v.score!=null)who.append(document.createTextNode(" "),makeBadge("score","SCORE "+v.score));if(v.verdict)who.append(document.createTextNode(" "),makeBadge(v.verdict,v.verdict));}
      const bubble=document.createElement("div");bubble.className="bubble";bubble.innerHTML=md(String(msg.text||""));item.append(who,bubble);nodes.push(item);
    });
  }else if(run.transcript){const card=document.createElement("div");card.className="card";const h=document.createElement("h2");h.textContent="transcript (pre-v0.2 run)";const body=document.createElement("div");body.innerHTML=md(run.transcript);card.append(h,body);nodes.push(card);}
  root.replaceChildren(...nodes);
}

async function show(){
  if(!selected)return;
  const resp=await fetch("/api/runs/"+selected);if(!resp.ok)return;
  const run=await resp.json(),m=run.meta,main=document.getElementById("main");
  const stick=main.scrollTop+main.clientHeight>=main.scrollHeight-60;
  ensureRunShell(run);updateSummary(run);renderMessages(run);
  const result=document.getElementById("resultCard"),body=document.getElementById("resultBody");
  result.hidden=!run.result.trim();document.getElementById("resultTitle").textContent=run.plan.trim()?"execution plan":"final result";body.innerHTML=md(run.result||"");
  const warnings=document.getElementById("warningCard"),warningBody=document.getElementById("warningBody");warnings.hidden=!(m.warnings||[]).length;warningBody.replaceChildren(...(m.warnings||[]).map(w=>{const line=document.createElement("div");line.textContent=w;return line;}));
  if(m.status==="running"&&stick)main.scrollTop=main.scrollHeight;
  clearInterval(timer);if(m.status==="running")timer=setInterval(()=>{show();loadRuns();},2000);
}

function pick(id){selected=id;renderedRun=null;loadRuns();show();}
document.getElementById("lead").addEventListener("change",()=>updateModelOptions("lead"));
document.getElementById("reviewer").addEventListener("change",()=>updateModelOptions("reviewer"));
document.getElementById("pSelect").addEventListener("change",event=>selectProject(event.target.value));
document.getElementById("newProjectButton").addEventListener("click",newProject);
document.getElementById("projectFolderButton").addEventListener("click",()=>chooseProjectFolder(false));
document.getElementById("runFolderButton").addEventListener("click",()=>chooseProjectFolder(true));
document.getElementById("saveProjectButton").addEventListener("click",()=>saveProject());
document.getElementById("loadModelsButton").addEventListener("click",loadModels);
document.getElementById("startRunButton").addEventListener("click",startRun);
loadProjects();loadModels();loadRuns();setInterval(loadRuns,5000);
</script>
</body>
</html>
"""
