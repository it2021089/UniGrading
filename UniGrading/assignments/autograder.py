# assignments/autograder.py
"""
Generic, adaptable autograder.

- Accepts: single files, zips/tars, notebooks, PDFs/DOCX/TXT/MD, images, binaries.
- For code projects: tries best-effort build/run in Docker (DinD), captures full logs.
- For apps (web/API): briefly boots and probes common ports inside the container, then stops.
- For non-runnable / missing deps: falls back to static + LLM review of code tree and logs.
- LLM produces summary + findings + grade + confidence; if confidence low, tasks.py can mark
  await_manual but still keep comments and full logs to help the professor.

Env:
- DOCKER_HOST=tcp://dind:2375
- AUTOGRADER_DISABLE_DOCKER=1 (skip docker)
- AUTOGRADER_ENABLE_LOCAL_EXEC=1 (unsafe local fallback for dev)
- AUTOGRADER_USE_LLM=1 + OPENAI_API_KEY
- OPENAI_MODEL (default gpt-5-mini)
- AUTOGRADER_TIMEOUT_SEC (default 180)
- AUTOGRADER_MAX_LOG_BYTES (default 200000)
- GRADER_SHARED_DIR (/grader-shared)
"""

from __future__ import annotations

import os, re, json, time, shutil, tarfile, zipfile, tempfile, mimetypes, subprocess, importlib
from pathlib import Path
from typing import Dict, Any, List, Optional, Tuple
from django.utils import timezone

def _try_import(name: str):
    try: return __import__(name)
    except Exception: return None

nbformat = _try_import("nbformat")
pdfminer_high = None
try:
    pdfminer_high = __import__("pdfminer.high_level", fromlist=["extract_text"])
except Exception:
    pdfminer_high = None
docx = _try_import("docx")
PIL = _try_import("PIL")
pytesseract = _try_import("pytesseract")
docker = _try_import("docker")

USE_LLM = os.getenv("AUTOGRADER_USE_LLM", "0") == "1" and bool(os.getenv("OPENAI_API_KEY"))
_openai = None
if USE_LLM:
    try:
        OpenAI = getattr(importlib.import_module("openai"), "OpenAI", None)
        _openai = OpenAI(api_key=os.getenv("OPENAI_API_KEY")) if OpenAI else None
    except Exception:
        _openai = None

DEFAULT_TIMEOUT = int(os.getenv("AUTOGRADER_TIMEOUT_SEC", "180"))
MAX_LOG = int(os.getenv("AUTOGRADER_MAX_LOG_BYTES", "200000"))

# ------------------ PUBLIC ------------------
def grade_submission(assignment, submission) -> Dict[str, Any]:
    start = time.time()
    logs: List[str] = []
    report: Dict[str, Any] = {"steps": []}

    spec_text = (getattr(assignment, "description", "") or "").strip()
    spec_attachment_text = ""
    try:
        a_file = getattr(assignment, "file", None)
        if a_file and a_file.name:
            spec_attachment_text = _read_django_file_text(a_file, logs)
    except Exception as e:
        logs.append(f"[warn] assignment attachment read failed: {e}")

    root = _ensure_shared_root(logs)
    tmp_dir = Path(tempfile.mkdtemp(prefix="autograde_", dir=str(root)))
    orig_name = Path(submission.file.name).name
    local_path = tmp_dir / orig_name
    try:
        f = submission.file.open("rb")
        with open(local_path, "wb") as out: shutil.copyfileobj(f, out)
        f.close()
    except Exception as e:
        logs.append(f"[error] could not download submission: {e}")
        return _final("failed", 0.0, "Could not read your file from storage.", report, _pack(logs), start)

    name = orig_name.lower()
    mt = mimetypes.guess_type(name)[0] or "application/octet-stream"
    report.update({"filename": name, "mimetype": mt, "shared_dir": str(tmp_dir)})

    try:
        if _is_archive(name):
            res = _handle_archive(tmp_dir, local_path, name, spec_text, spec_attachment_text, logs, report)
        elif name.endswith(".ipynb"):
            res = _handle_notebook(tmp_dir, local_path, spec_text, spec_attachment_text, logs, report)
        elif _looks_like_code(name):
            res = _handle_single_code(tmp_dir, local_path, name, spec_text, spec_attachment_text, logs, report)
        elif name.endswith(".pdf") or "pdf" in mt:
            res = _llm_grade_textual(_extract_pdf(local_path, logs), spec_text, spec_attachment_text, {"type":"pdf"}, logs, report)
        elif name.endswith(".docx") or "word" in mt:
            res = _llm_grade_textual(_extract_docx(local_path, logs), spec_text, spec_attachment_text, {"type":"docx"}, logs, report)
        elif name.endswith(".txt") or name.endswith(".md") or mt.startswith("text/"):
            res = _llm_grade_textual(_safe_read(local_path, logs), spec_text, spec_attachment_text, {"type":"text"}, logs, report)
        elif _looks_like_img(name, mt):
            res = _llm_grade_textual(_extract_img(local_path, logs), spec_text, spec_attachment_text, {"type":"image"}, logs, report)
        else:
            res = _llm_grade_textual(_peek_bin(local_path, logs), spec_text, spec_attachment_text, {"type":"binary"}, logs, report)
    except Exception as e:
        logs.append(f"[error] pipeline crashed: {e}")
        res = _final("failed", 5.0, "We could not analyze your file; please re-check submission.", report, _pack(logs), start)

    try: shutil.rmtree(tmp_dir, ignore_errors=True)
    except Exception: pass

    return res

def apply_result_to_submission(submission, result: Dict[str, Any]) -> None:
    if hasattr(submission, "grade_pct"):
        g = result.get("grade_pct", None)
        submission.grade_pct = float(g) if g is not None else None
    if hasattr(submission, "ai_feedback"):
        submission.ai_feedback = result.get("feedback", "") or ""
    if hasattr(submission, "autograde_status"):
        submission.autograde_status = result.get("status", "done")
    if hasattr(submission, "autograde_report"):
        submission.autograde_report = result.get("report", {}) or {}
    if hasattr(submission, "runner_logs"):
        submission.runner_logs = result.get("logs", "") or ""

# ------------------ TYPE ROUTERS ------------------
def _is_archive(n: str) -> bool:
    n = n.lower()
    return n.endswith(".zip") or n.endswith(".tar") or n.endswith(".tar.gz") or n.endswith(".tgz")

def _looks_like_code(n: str) -> bool:
    return n.lower().endswith((".py",".sh",".js",".ts",".java",".c",".cc",".cpp",".go",".rs",".rb",".php",".cs"))

def _looks_like_img(n: str, mt: str) -> bool:
    return n.lower().endswith((".png",".jpg",".jpeg",".gif",".bmp",".webp",".tiff",".svg")) or (mt and mt.startswith("image/"))

def _handle_archive(tmp_dir: Path, local: Path, filename: str, spec: str, attach: str, logs: List[str], report: Dict[str,Any]) -> Dict[str,Any]:
    work = tmp_dir / "work"
    work.mkdir(exist_ok=True)
    try:
        if filename.endswith(".zip"):
            with zipfile.ZipFile(local, "r") as zf: zf.extractall(work)
        else:
            with tarfile.open(local, "r:*") as tf: tf.extractall(work)
        logs.append(f"[ok] extracted archive -> {work}")
    except Exception as e:
        logs.append(f"[error] archive extract failed: {e}")
        return _llm_grade_textual(_peek_bin(local, logs), spec, attach, {"type":"archive-unreadable"}, logs, report)

    report["file_tree"] = _list_files(work)[:3000]

    if nbformat:
        nbs = list(work.rglob("*.ipynb"))
        if nbs:
            return _handle_notebook(tmp_dir, nbs[0], spec, attach, logs, report, sourced=True)

    # If any report-only files inside:
    has_report = any(p.suffix.lower() in (".pdf",".docx",".txt",".md") for p in work.rglob("*") if p.is_file())
    if has_report:
        snap = _snapshot_text(work, logs)
        return _llm_grade_textual(snap, spec, attach, {"type":"archive-report"}, logs, report)

    langs = _detect_langs(work)
    report["languages"] = langs
    if langs:
        primary = langs[0]["language"]
        return _build_run_project(work, primary, spec, attach, logs, report)

    snap = _snapshot_text(work, logs)
    return _llm_grade_textual(snap, spec, attach, {"type":"archive-static"}, logs, report)

def _handle_notebook(tmp_dir: Path, nb_path: Path|str, spec: str, attach: str, logs: List[str], report: Dict[str,Any], sourced: bool=False)->Dict[str,Any]:
    if not nbformat:
        return _llm_grade_textual(_safe_read(nb_path, logs), spec, attach, {"type":"ipynb-static"}, logs, report)
    run_dir = tmp_dir / "nb_run"; run_dir.mkdir(exist_ok=True)
    nb_in = Path(nb_path) if sourced else run_dir / "notebook.ipynb"
    if not sourced: shutil.copy2(nb_path, nb_in)

    try:
        cmd = [os.getenv("PYTHON","python"), "-m", "jupyter", "nbconvert", "--to","notebook",
               "--execute", str(nb_in), f"--ExecutePreprocessor.timeout={min(240,DEFAULT_TIMEOUT)}", "--output","executed.ipynb"]
        cp = subprocess.run(cmd, cwd=nb_in.parent, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=DEFAULT_TIMEOUT+60)
        out = cp.stdout[-20000:]
        logs.append(out)
        executed = nb_in.parent/"executed.ipynb"
        txt = ""
        if executed.exists():
            try:
                nb2 = nbformat.read(executed, as_version=4)
                txt = _nb_text(nb2)
            except Exception as e: logs.append(f"[warn] executed nb parse failed: {e}")
        res = _llm_grade_textual(f"NOTEBOOK OUTPUT:\n{txt}\n\nRUN LOG TAIL:\n{out[-4000:]}", spec, attach, {"type":"ipynb-exec"}, logs, report)
        if cp.returncode==0:
            res["feedback"] = (res.get("feedback","")+"\n\nBonus: notebook executed successfully.").strip()
            res["grade_pct"] = max(res.get("grade_pct",0), 80.0)
        else:
            res["feedback"] = (res.get("feedback","")+"\n\nNote: execution errors occurred; graded leniently.").strip()
            res["grade_pct"] = max(res.get("grade_pct",0), 55.0)
        return res
    except Exception as e:
        logs.append(f"[warn] notebook exec failed: {e}")
        return _llm_grade_textual(_safe_read(nb_in, logs), spec, attach, {"type":"ipynb-static"}, logs, report)

def _handle_single_code(tmp_dir: Path, local: Path, filename: str, spec: str, attach: str, logs: List[str], report: Dict[str,Any])->Dict[str,Any]:
    lang = _ext2lang(Path(filename).suffix.lower())
    ran, ok, out = _run_single(local, lang, DEFAULT_TIMEOUT)
    logs.append(out[-20000:])
    res = _llm_grade_textual(f"RUNTIME OUTPUT TAIL:\n{out[-6000:]}", spec, attach, {"type":f"single-{lang or 'code'}"}, logs, report)
    missing = ("No such file or directory" in out) or ("can't open file" in out) or ("ModuleNotFoundError" in out and lang=="python")
    if ran and ok:
        res["grade_pct"] = max(res.get("grade_pct",0), 75.0); res["feedback"]=(res.get("feedback","")+"\n\nProgram ran successfully.").strip()
    elif missing:
        res["grade_pct"] = min(res.get("grade_pct",100.0), 5.0); res["status"]="failed"
        res["feedback"]="No runnable entry found. Please upload a correct entry point (e.g., .py or proper build)."
    elif ran:
        res["grade_pct"] = min(res.get("grade_pct",100.0), 25.0); res["feedback"]=(res.get("feedback","")+"\n\nProgram executed with errors.").strip()
    else:
        res["grade_pct"] = min(res.get("grade_pct",100.0), 15.0); res["feedback"]=(res.get("feedback","")+"\n\nCould not execute; static review only.").strip()
    return res

# ------------------ PROJECT RUN ------------------
def _build_run_project(workdir: Path, lang: str, spec: str, attach: str, logs: List[str], report: Dict[str,Any])->Dict[str,Any]:
    ran, ok, out = _run_project(workdir, lang, DEFAULT_TIMEOUT)
    logs.append(out[-40000:])
    tree = "\n".join(report.get("file_tree", [])[:2000])
    res = _llm_grade_textual(f"BUILD/RUN LOG TAIL:\n{out[-8000:]}\n\nFILE TREE (first 2000 lines):\n{tree}",
                             spec, attach, {"type":f"project-{lang}"}, logs, report)
    if ran and ok:
        res["feedback"]=(res.get("feedback","")+"\n\nProject built and ran.").strip()
        res["grade_pct"]=max(res.get("grade_pct",0), 80.0)
    elif ran:
        res["feedback"]=(res.get("feedback","")+"\n\nBuild/run issues; graded leniently.").strip()
        res["grade_pct"]=max(res.get("grade_pct",0), 60.0)
    else:
        res["feedback"]=(res.get("feedback","")+"\n\nCould not build/run; static review only.").strip()
        res["grade_pct"]=max(res.get("grade_pct",0), 50.0)
    return res

def _run_single(path: Path, lang: Optional[str], timeout: int) -> Tuple[bool,bool,str]:
    if os.getenv("AUTOGRADER_DISABLE_DOCKER")=="1" and os.getenv("AUTOGRADER_ENABLE_LOCAL_EXEC")!="1":
        return False, False, "[safe] execution disabled"
    try:
        if docker and os.getenv("AUTOGRADER_DISABLE_DOCKER")!="1": return _docker_single(path, lang, timeout)
        if os.getenv("AUTOGRADER_ENABLE_LOCAL_EXEC")=="1": return _local_single(path, lang, timeout)
    except Exception as e:
        return True, False, f"[error] runner crashed: {e}"
    return False, False, "[safe] no runner available"

def _run_project(workdir: Path, lang: str, timeout: int) -> Tuple[bool,bool,str]:
    if os.getenv("AUTOGRADER_DISABLE_DOCKER")=="1" and os.getenv("AUTOGRADER_ENABLE_LOCAL_EXEC")!="1":
        return False, False, "[safe] execution disabled"
    try:
        if docker and os.getenv("AUTOGRADER_DISABLE_DOCKER")!="1": return _docker_project(workdir, lang, timeout)
        if os.getenv("AUTOGRADER_ENABLE_LOCAL_EXEC")=="1": return _local_project(workdir, lang, timeout)
    except Exception as e:
        return True, False, f"[error] runner crashed: {e}"
    return False, False, "[safe] no runner available"

# ---- Docker helpers ----
def _image(lang: Optional[str], project=False) -> str:
    if lang in ("python", None): return os.getenv("AUTOGRADER_DOCKER_IMAGE_DS" if project else "AUTOGRADER_DOCKER_IMAGE_PY", "python:3.11")
    return {
        "node":"node:20", "java":"maven:3.9-eclipse-temurin-17", "c":"gcc:13", "cpp":"gcc:13",
        "go":"golang:1.22","rust":"rust:1.79","ruby":"ruby:3.3","php":"php:8.3-cli","dotnet":"mcr.microsoft.com/dotnet/sdk:8.0"
    }.get(lang, "python:3.11")

def _cmd_single(file_in: str, lang: Optional[str]) -> List[str]:
    if lang=="python" or file_in.endswith(".py"): return ["python", file_in]
    if lang=="bash" or file_in.endswith(".sh"):  return ["bash", file_in]
    if lang=="node" or file_in.endswith((".js",".ts")): return ["sh","-lc",f"node {shq(file_in)} || (echo 'no node entry' && false)"]
    return ["sh","-lc", f"echo 'No direct runner for {shq(file_in)}' && false"]

def _cmd_project(lang: str) -> List[str]:
    # Boot briefly & probe common local ports; then stop.
    probe = "for p in 3000 5000 5173 8000 8080 4200; do (curl -s -I http://127.0.0.1:$p || true); done"
    if lang=="python":
        return ["sh","-lc","python -V; if [ -f requirements.txt ]; then pip -q install -r requirements.txt || true; fi; "
                         "(pytest -q || true); (python app.py || python main.py || true) & sleep 7; "+probe+"; pkill -f python || true; true"]
    if lang=="node":
        return ["sh","-lc","node -v; if [ -f package.json ]; then (npm ci --silent || npm i --silent) || true; "
                         "(npm test --silent || true); (npm run start --silent || node . || true) & sleep 7; "+probe+"; pkill -f node || true; true"]
    if lang=="java":
        return ["sh","-lc","(test -f mvnw && chmod +x mvnw || true); if [ -f pom.xml ]; then (./mvnw -q -DskipTests package || mvn -q -DskipTests package || true); "
                         "(java -jar target/*.jar || true) & sleep 8; "+probe+"; pkill -f java || true; true; else echo 'no pom.xml'; fi"]
    if lang in ("c","cpp"):
        return ["sh","-lc","(test -f Makefile && make -s || (find -name '*.c' -o -name '*.cpp' | xargs -r gcc -O2 -std=c11 -o app && ./app || true)) || true"]
    if lang=="go":
        return ["sh","-lc","go version; (go test ./... || true); (go run . || true) & sleep 7; "+probe+"; pkill -f go || true; true"]
    if lang=="rust":
        return ["sh","-lc","cargo --version; (cargo test -q || true); (cargo run -q || true) & sleep 8; "+probe+"; pkill -f cargo || true; true"]
    if lang=="dotnet":
        return ["sh","-lc","dotnet --info; (dotnet build -clp:ErrorsOnly || true); (dotnet test -l:trx || true); true"]
    if lang=="ruby":
        return ["sh","-lc","ruby -v; (ruby main.rb || true) & sleep 6; "+probe+"; pkill -f ruby || true; true"]
    if lang=="php":
        return ["sh","-lc","php -v; (php -S 127.0.0.1:8000 -t . & sleep 6; "+probe+"; pkill -f 'php -S' || true) || true"]
    return ["sh","-lc","echo 'No project runner for this language'; true"]

def _docker_single(path: Path, lang: Optional[str], timeout: int)->Tuple[bool,bool,str]:
    client = docker.from_env()
    vols = {str(path.parent): {"bind": "/work", "mode": "ro"}}
    c = client.containers.run(_image(lang), _cmd_single("/work/"+path.name, lang),
                              detach=True, working_dir="/work", network_mode="none",
                              mem_limit="768m", nano_cpus=1_000_000_000, volumes=vols)
    try: ok = _wait_or_kill(c, timeout)
    finally:
        out = _logs(c); _rm(c)
    return True, ok, out

def _docker_project(workdir: Path, lang: str, timeout: int)->Tuple[bool,bool,str]:
    client = docker.from_env()
    vols = {str(workdir): {"bind": "/work", "mode": "rw"}}
    c = client.containers.run(_image(lang, True), _cmd_project(lang),
                              detach=True, working_dir="/work", network_mode="none",
                              mem_limit="2g", nano_cpus=2_000_000_000, volumes=vols)
    try: ok = _wait_or_kill(c, timeout)
    finally:
        out = _logs(c); _rm(c)
    return True, ok, out

def _wait_or_kill(container, timeout:int)->bool:
    start=time.time()
    while True:
        try:
            container.reload()
            st = container.attrs.get("State", {})
            if st.get("Status") in ("exited","dead"):
                return int(st.get("ExitCode",1) or 1)==0
        except Exception:
            break
        if time.time()-start>timeout:
            try: container.kill()
            except Exception: pass
            return False
        time.sleep(1.0)

def _logs(container)->str:
    try:
        raw = container.logs(stdout=True, stderr=True)
        s = raw.decode("utf-8","ignore") if isinstance(raw,bytes) else str(raw)
        return s[-MAX_LOG:]
    except Exception:
        return ""

def _rm(container):
    try: container.remove(force=True)
    except Exception: pass

# ---- local (unsafe) ----
def _local_single(path:Path, lang:Optional[str], timeout:int)->Tuple[bool,bool,str]:
    cp = subprocess.run(_cmd_single(str(path),lang), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=timeout)
    return True, (cp.returncode==0), cp.stdout

def _local_project(workdir:Path, lang:str, timeout:int)->Tuple[bool,bool,str]:
    cp = subprocess.run(_cmd_project(lang), cwd=workdir, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=timeout)
    return True, (cp.returncode==0), cp.stdout

# ------------------ TEXT / SNAPSHOT ------------------
def _read_django_file_text(dj_file, logs:List[str])->str:
    name = Path(dj_file.name).name.lower()
    mt = mimetypes.guess_type(name)[0] or ""
    tmp = _temp_in_shared(f"spec_{name}")
    try:
        f = dj_file.open("rb")
        with open(tmp,"wb") as out: shutil.copyfileobj(f,out)
        f.close()
    except Exception as e:
        logs.append(f"[warn] attach download failed: {e}"); return ""
    try:
        if name.endswith(".pdf") or "pdf" in mt: return _extract_pdf(tmp, logs)
        if name.endswith(".docx") or "word" in mt: return _extract_docx(tmp, logs)
        if name.endswith(".txt") or name.endswith(".md") or mt.startswith("text/"): return _safe_read(tmp, logs)
    finally:
        try: tmp.unlink(missing_ok=True)
        except Exception: pass
    return ""

def _extract_pdf(p:Path|str, logs)->str:
    if not pdfminer_high: logs.append("[info] pdfminer not installed"); return ""
    try: return pdfminer_high.extract_text(str(p)) or ""
    except Exception as e: logs.append(f"[warn] pdf parse failed: {e}"); return ""

def _extract_docx(p:Path|str, logs)->str:
    if not docx: logs.append("[info] python-docx not installed"); return ""
    try:
        d = docx.Document(str(p))
        return "\n".join(par.text for par in d.paragraphs)
    except Exception as e: logs.append(f"[warn] docx parse failed: {e}"); return ""

def _extract_img(p:Path|str, logs)->str:
    if not PIL: logs.append("[info] Pillow not installed"); return ""
    try:
        from PIL import Image
        im = Image.open(str(p))
        meta = f"(image {im.size} {im.mode})"
        if pytesseract:
            try: return meta+"\n\n"+pytesseract.image_to_string(im)
            except Exception as e: logs.append(f"[warn] OCR failed: {e}"); return meta
        return meta
    except Exception as e: logs.append(f"[warn] image open failed: {e}"); return ""

def _safe_read(p:Path|str, logs, limit=200_000)->str:
    try:
        with open(p,"r",encoding="utf-8",errors="ignore") as f: return f.read(limit)
    except Exception as e: logs.append(f"[warn] text read failed: {e}"); return ""

def _peek_bin(p:Path|str, logs, limit=64)->str:
    try:
        with open(p,"rb") as f: b=f.read(limit)
        return f"(binary peek, {limit} bytes hex) {b.hex()}"
    except Exception as e: logs.append(f"[warn] binary peek failed: {e}"); return ""

def _snapshot_text(root:Path, logs, limit_bytes=200_000)->str:
    total=0; out=[]
    for p in root.rglob("*"):
        if not p.is_file(): continue
        if p.suffix.lower() in (".png",".jpg",".jpeg",".gif",".bmp",".webp",".tiff",".pdf"): continue
        try:
            if p.stat().st_size>60_000: continue
            txt = _safe_read(p, logs, 60_000)
            if txt:
                rel = str(p.relative_to(root))
                out.append(f"\n--- {rel} ---\n{txt}\n")
                total += len(txt)
                if total>limit_bytes: break
        except Exception: pass
    return "".join(out)

def _nb_text(nb)->str:
    lines=[]
    for cell in nb.cells:
        if getattr(cell,"cell_type","")=="code":
            for o in cell.get("outputs",[]):
                if "text" in o: lines.append(o["text"])
                if "data" in o and "text/plain" in o["data"]: lines.append(o["data"]["text/plain"])
    return "\n".join(lines)[:200_000]

def _detect_langs(root:Path)->List[Dict[str,Any]]:
    score={}
    for p in root.rglob("*"):
        if not p.is_file(): continue
        ext=p.suffix.lower(); lang=_ext2lang(ext)
        if lang: score[lang]=score.get(lang,0)+1
        if p.name.lower() in ("pom.xml","build.gradle","package.json","requirements.txt","pyproject.toml","makefile","cmakelists.txt"):
            score[p.name.lower()]=score.get(p.name.lower(),0)+5
    return [{"language":k,"score":v} for k,v in sorted(score.items(), key=lambda x:x[1], reverse=True)]

def _ext2lang(ext:str)->Optional[str]:
    return {".py":"python",".ipynb":"python",".sh":"bash",".js":"node",".ts":"node",".java":"java",".c":"c",".cc":"cpp",".cpp":"cpp",".go":"go",".rs":"rust",".rb":"ruby",".php":"php",".cs":"dotnet"}.get(ext)

# ------------------ LLM ------------------
SYS = ("You are a fair, careful grader. Use only provided evidence (logs, code snapshot, text). "
       "If evidence is insufficient to assign a confident numeric grade, set needs_manual=true and still provide a helpful summary and findings. "
       "Keep feedback concise and actionable.")

def _llm_grade_textual(student_text:str, spec_text:str, spec_attach:str, context:Dict[str,Any], logs:List[str], report:Dict[str,Any])->Dict[str,Any]:
    report["detected_work"]=report.get("detected_work",False) or bool(student_text.strip())
    if _openai and USE_LLM:
        try:
            prompt=f"""
Context: {json.dumps(context)}
Assignment (free-form):
<<<
{spec_text[:6000]}
>>>

Attachment (if any):
<<<
{spec_attach[:4000]}
>>>

Evidence (logs/text/code snapshot):
<<<
{(student_text or '')[:12000]}
>>>

Return STRICT JSON:
{{
  "summary": "2-5 sentences of what exists and what it does",
  "findings": ["bullet points of issues/strengths"],
  "grade_pct": 0-100 float,
  "confidence": 0-1 float,
  "needs_manual": true|false
}}
"""
            r=_openai.chat.completions.create(model=os.getenv("OPENAI_MODEL","gpt-5-mini"),
                                              messages=[{"role":"system","content":SYS},{"role":"user","content":prompt}])
            txt=r.choices[0].message.content or "{}"
            data=_json_from(txt)
            grade=float(data.get("grade_pct",60.0))
            fb=data.get("summary","").strip()
            finds=data.get("findings",[])
            if finds: fb=(fb+"\n\nNotes:\n- "+"\n- ".join(finds)).strip()
            report["llm_used"]=True
            report["llm_confidence"]=data.get("confidence",0.5)
            report["llm_needs_manual"]=bool(data.get("needs_manual",False))
            return _final("done", _clamp(grade), fb, report, _pack(logs), time.time())
        except Exception as e:
            logs.append(f"[warn] LLM failed: {e}")
            report["llm_used"]=False
            report["llm_error"]=str(e)
    # Heuristic fallback
    base=10.0 if not student_text.strip() else 55.0
    if len(student_text)>2000: base=max(base,65.0)
    fb=("Heuristic review (LLM unavailable). We analyzed files/logs; this is a preliminary score; instructor may adjust.")
    return _final("partial" if student_text.strip() else "failed", _clamp(base), fb, report, _pack(logs), time.time())

# ------------------ misc ------------------
def _json_from(s:str)->Dict[str,Any]:
    m=re.search(r"\{.*\}", s, flags=re.S)
    if not m: return {}
    try: return json.loads(m.group(0))
    except Exception: return {}

def _ensure_shared_root(logs:List[str])->Path:
    for cand in (os.getenv("GRADER_SHARED_DIR"), "/grader-shared", "/tmp/autograder"):
        if not cand: continue
        d=Path(cand)
        try:
            d.mkdir(parents=True, exist_ok=True)
            if os.access(d, os.W_OK): return d
        except Exception as e: logs.append(f"[warn] cannot use {d}: {e}")
    return Path(tempfile.gettempdir())

def _temp_in_shared(name:str)->Path:
    base=_ensure_shared_root([])
    p=base/f"_tmp_{int(time.time()*1000)}_{name}"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p

def _pack(chunks:List[str])->str:
    return ("\n".join(chunks))[-MAX_LOG:]

def _clamp(x:float,a:float=0.0,b:float=100.0)->float:
    return max(a,min(b,x))

def shq(s:str)->str:
    return "'" + s.replace("'","'\"'\"'") + "'"

def _final(status:str, grade:float, feedback:str, report:Dict[str,Any], logs:str, start:float)->Dict[str,Any]:
    return {
        "status": status,
        "grade_pct": _clamp(grade),
        "feedback": feedback.strip(),
        "report": report,
        "logs": logs,
        "finished_at": timezone.now().isoformat(),
        "elapsed_s": max(0.0, time.time()-start),
    }
