# assignments/autograder.py
"""
Adaptive, AI-planned autograder.

Flow
----
1) Download submission (or extract archive) into a shared workroot (for Docker bind).
2) Build a full project TREE (recursive, up to 20k files) + key hints (e.g., manage.py, pom.xml).
3) Ask an LLM to produce a minimal "run plan" (list of services) describing:
      - image (Docker image)
      - workdir (relative path within tree)
      - setup (list of shell commands, e.g., pip install -r requirements.txt)
      - run (list of shell commands to exercise the app/tests)
      - env (optional environment variables)
      - network (bool; defaults False; honored only if AUTOGRADER_ALLOW_NET_SETUP=1)
      - timeout (int seconds; clamped)
   We sanitize the plan to a safe subset and restrict images to an allowlist.
4) Execute services sequentially with Docker. Capture all logs.
5) If overall run fails, send TREE + logs + original plan back to the LLM ONCE to get a "refined plan"; re-run.
6) Grade leniently by LLM using the logs, tree snapshot, and assignment description; or heuristic if LLM unavailable.

Environment flags
-----------------
AUTOGRADER_USE_LLM=1           Enable LLM usage (requires OPENAI_API_KEY)
AUTOGRADER_REQUIRE_LLM=1       If set, tasks.py may hold grade when LLM unusable (this file still returns a result)
OPENAI_MODEL                   Chat model (default: gpt-5-mini)
GRADER_SHARED_DIR              Path for shared temp (default: /grader-shared)
AUTOGRADER_ALLOW_NET_SETUP=1   Allow containers to access network during setup/run (default: off)
AUTOGRADER_IMAGE_DEFAULT       Default image when plan's image not allowed (default: python:3.11)
AUTOGRADER_ALLOWED_IMAGES      Comma-separated allowlist override; else default list below

Safety
------
- Network is OFF by default (network_mode="none"). Only enabled if the plan asks for it AND AUTOGRADER_ALLOW_NET_SETUP=1.
- No apt-get is suggested; planner is nudged toward pip/npm/maven/gradle tasks.
- Images are restricted to an allowlist to prevent arbitrary pulls.

Result schema
-------------
{
  "status": "done" | "partial" | "failed",
  "grade_pct": float,
  "feedback": str,
  "report": {...},         # languages, file_tree, plans, etc.
  "logs": str,             # full combined logs (truncated)
  "finished_at": iso8601,
  "elapsed_s": float
}
"""

from __future__ import annotations

import os, re, io, json, time, shutil, tarfile, zipfile, tempfile, mimetypes, subprocess, importlib
from pathlib import Path
from typing import Dict, Any, List, Optional, Tuple

from django.utils import timezone

# -----------------------
# Optional imports
# -----------------------
def _try_import(name: str):
    try:
        return __import__(name)
    except Exception:
        return None

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

# -----------------------
# Config & toggles
# -----------------------
USE_LLM = os.getenv("AUTOGRADER_USE_LLM", "0") == "1" and bool(os.getenv("OPENAI_API_KEY"))
REQUIRE_LLM = os.getenv("AUTOGRADER_REQUIRE_LLM", "1") == "1"
ALLOW_NET = os.getenv("AUTOGRADER_ALLOW_NET_SETUP", "0") == "1"

DEFAULT_IMAGE = os.getenv("AUTOGRADER_IMAGE_DEFAULT", "python:3.11")

# Allowed images (assignment-agnostic but safety-constrained); override with AUTOGRADER_ALLOWED_IMAGES
_default_allow = [
    "python:3.12", "python:3.11", "python:3.10",
    "node:20", "node:18",
    "maven:3.9-eclipse-temurin-17",
    "gradle:8.8-jdk17",
    "gcc:13",
    "golang:1.22",
    "rust:1.79",
    "ruby:3.3",
    "php:8.3-cli",
    "mcr.microsoft.com/dotnet/sdk:8.0"
]
_allowed_env = os.getenv("AUTOGRADER_ALLOWED_IMAGES", "")
ALLOWED_IMAGES = [s.strip() for s in _allowed_env.split(",") if s.strip()] or _default_allow

# OpenAI client
_openai_client = None
if USE_LLM:
    try:
        openai_mod = importlib.import_module("openai")
        OpenAI = getattr(openai_mod, "OpenAI", None)
        if OpenAI:
            _openai_client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
        else:
            _openai_client = openai_mod  # legacy
    except Exception:
        _openai_client = None

# -----------------------
# Public API
# -----------------------
def grade_submission(assignment, submission) -> Dict[str, Any]:
    start = time.time()
    logs: List[str] = []
    report: Dict[str, Any] = {"steps": []}

    # Assignment context
    spec_text = (getattr(assignment, "description", "") or "").strip()
    spec_attachment_text = ""
    try:
        a_file = getattr(assignment, "file", None)
        if a_file and a_file.name:
            spec_attachment_text = _extract_text_from_arbitrary_file(a_file, logs)
    except Exception as e:
        logs.append(f"[warn] Failed reading assignment attachment: {e}")

    # Prepare shared workroot
    try:
        shared_root = Path(os.getenv("GRADER_SHARED_DIR", "/grader-shared"))
        shared_root.mkdir(parents=True, exist_ok=True)
    except Exception as e:
        logs.append(f"[warn] Could not ensure shared dir; fallback to tmp: {e}")
        shared_root = Path(tempfile.gettempdir())

    workroot = Path(tempfile.mkdtemp(prefix="autograde_", dir=str(shared_root)))
    orig_name = Path(submission.file.name).name
    local_path = workroot / orig_name
    try:
        with submission.file.open("rb") as f, open(local_path, "wb") as out:
            shutil.copyfileobj(f, out)
    except Exception as e:
        logs.append(f"[error] Could not read submission from storage: {e}")
        return _final("failed", 0.0, "Could not read your file from storage.", report, "\n".join(logs), start)

    # Decide: archive vs single file vs doc
    name = orig_name.lower()
    mimetype = mimetypes.guess_type(name)[0] or "application/octet-stream"
    report["filename"] = name
    report["mimetype"] = mimetype
    report["workroot"] = str(workroot)

    try:
        if _is_archive(name):
            result = _handle_archive_with_ai_plan(workroot, local_path, name, spec_text, spec_attachment_text, logs, report)
        elif name.endswith(".ipynb"):
            result = _handle_notebook(workroot, local_path, name, spec_text, spec_attachment_text, logs, report)
        elif _looks_like_code(name):
            result = _handle_single_code(workroot, local_path, name, spec_text, spec_attachment_text, logs, report)
        elif name.endswith(".pdf") or "pdf" in mimetype:
            text = _extract_text_from_pdf(local_path, logs)
            result = _llm_grade_textual(text, spec_text, spec_attachment_text, {"type": "pdf"}, logs, report)
        elif name.endswith(".docx") or "word" in mimetype:
            text = _extract_text_from_docx(local_path, logs)
            result = _llm_grade_textual(text, spec_text, spec_attachment_text, {"type": "docx"}, logs, report)
        elif name.endswith(".txt") or name.endswith(".md") or "text" in mimetype:
            text = _safe_read_text(local_path, logs)
            result = _llm_grade_textual(text, spec_text, spec_attachment_text, {"type": "text"}, logs, report)
        elif _looks_like_image(name, mimetype):
            text = _extract_text_from_image(local_path, logs)
            result = _llm_grade_textual(text, spec_text, spec_attachment_text, {"type": "image"}, logs, report)
        else:
            text = _best_effort_binary_peek(local_path, logs)
            result = _llm_grade_textual(text, spec_text, spec_attachment_text, {"type": "binary"}, logs, report)
    except Exception as e:
        logs.append(f"[error] Pipeline crashed: {e}")
        result = _final("failed", 5.0, "We could not analyze your file; please re-check submission.", report, "\n".join(logs), start)

    # Cleanup
    try:
        shutil.rmtree(workroot, ignore_errors=True)
    except Exception:
        pass

    # Leniency floor if work detected
    if result["status"] in ("done", "partial") and result.get("grade_pct", 0) < 40 and report.get("detected_work", False):
        result["logs"] += "\n[policy] Leniency floor applied because work was detected."
        result["grade_pct"] = max(result.get("grade_pct", 0), 40.0)

    return result


def apply_result_to_submission(submission, result: Dict[str, Any]) -> None:
    if hasattr(submission, "grade_pct"):
        grade = result.get("grade_pct", None)
        submission.grade_pct = float(grade) if grade is not None else None
    if hasattr(submission, "ai_feedback"):
        submission.ai_feedback = str(result.get("feedback", ""))
    if hasattr(submission, "autograde_status"):
        submission.autograde_status = result.get("status", "done")
    if hasattr(submission, "autograde_report"):
        try:
            submission.autograde_report = result.get("report", {})
        except Exception:
            submission.autograde_report = {}
    if hasattr(submission, "runner_logs"):
        submission.runner_logs = str(result.get("logs", ""))

# -----------------------
# Core: AI-planned archive/project handling
# -----------------------
def _handle_archive_with_ai_plan(workroot: Path, local_path: Path, filename: str, spec_text: str, spec_attach: str,
                                 logs: List[str], report: Dict[str, Any]) -> Dict[str, Any]:
    # Extract
    projdir = workroot / "work"
    projdir.mkdir(exist_ok=True)
    report["detected_work"] = True
    try:
        if filename.endswith(".zip"):
            with zipfile.ZipFile(local_path, "r") as zf:
                zf.extractall(projdir)
        else:
            with tarfile.open(local_path, "r:*") as tf:
                tf.extractall(projdir)
        logs.append(f"[ok] Archive extracted into {projdir}")
        logs.append(f"Professor: tree_root => {projdir}")
    except Exception as e:
        logs.append(f"[error] Could not extract archive: {e}")
        snapshot = _best_effort_binary_peek(local_path, logs)
        return _llm_grade_textual(snapshot, spec_text, spec_attach, {"type": "archive-corrupt"}, logs, report)

    # Inventory
    files = _list_files(projdir)
    report["file_tree"] = files[:20000]
    langs = _detect_languages(projdir)
    report["languages"] = langs
    tree_summary = _compose_tree_summary(projdir, files)
    tree_full = "\n".join(files)
    report["tree_full_count"] = len(files)
    report["candidate_roots"] = _candidate_roots(projdir)
    if report["candidate_roots"]:
        logs.append("Professor: candidate_roots => " + ", ".join(report["candidate_roots"][:20]))

    # If notebook present, shortcut to notebook executor
    nb_files = [p for p in _iter_paths(projdir) if p.suffix.lower() == ".ipynb"]
    if nb_files and nbformat:
        best_nb = nb_files[0]
        return _handle_notebook(workroot, best_nb, best_nb.name, spec_text, spec_attach, logs, report, sourced=True)

    # --- AI plan (first pass)
    plan, plan_err = _plan_with_ai(projdir, tree_full, spec_text, report["candidate_roots"], logs)
    if plan_err:
        logs.append(f"[warn] Planner fallback due to: {plan_err}")
        plan = _fallback_plan(projdir)  # generic

    report["plan_initial"] = plan

    ok, run_logs = _run_services_plan(projdir, plan)
    full = run_logs[-200000:]
    logs.append(full)
    report["sandbox_full_log"] = full
    # Track the *latest* run log for grading:
    last_run_log = full
    report["sandbox_last_log"] = last_run_log

    # If failed, try ONE refinement with AI
    if not ok and USE_LLM and _openai_client:
        ref_plan, ref_err = _refine_plan_with_ai(projdir, tree_full, plan, full, report["candidate_roots"], logs)
        if not ref_err and ref_plan:
            report["plan_refined"] = ref_plan
            ok2, run_logs2 = _run_services_plan(projdir, ref_plan)
            full2 = run_logs2[-200000:]

            logs[:] = ["=== RE-RUN (refined) ===\n" + full2]
            report["sandbox_full_log"] = full2

            ok = ok2
        else:
            logs.append(f"[warn] Refine plan failed: {ref_err or 'no plan returned'}")

    # Grade via LLM (or heuristic) using ONLY the latest run’s output
    context = {"type": "ai-plan", "ok": ok, "languages": langs}
    grade_text = _compose_grade_context(tree_summary, report.get("sandbox_last_log", ""))
    res = _llm_grade_textual(grade_text, spec_text, spec_attach, context, logs, report)

    # Small bonus if ok
    # if ok:
    #     res["grade_pct"] = max(res.get("grade_pct", 0), 80.0)
    #     res["feedback"] = (res.get("feedback", "") + "\n\nProject executed successfully under AI-planned run.").strip()
    # else:
    #     res["feedback"] = (res.get("feedback", "") + "\n\nWe could not fully run the project; graded based on files/logs.").strip()

    return res

# -----------------------
# Planner (AI)
# -----------------------
PLANNER_SYSTEM = """You are a build/run planner for arbitrary student projects.
- Output a concise JSON plan with a list of services to run in Docker.
- Prefer minimal setup using language-native tools (pip, npm, maven/gradle, pytest, unittest).
- Avoid OS package managers (no apt-get).
- Do not start long-running servers; instead run checks/tests or one-shot scripts.
- Assume no internet access unless you explicitly mark 'network': true (installer steps may still fail if network is blocked).
- Keep timeouts modest (<= 240s).
Schema:
{
  "services": [
    {
      "name": "string",
      "image": "docker-image",
      "workdir": "relative/path/within/tree or '.'",
      "setup": ["cmd", "..."],        # optional
      "run":   ["cmd", "..."],        # at least one
      "env":   {"KEY":"VALUE"},       # optional
      "network": false,               # optional, default false
      "timeout": 180                  # optional, int seconds
    }
  ]
}
Return ONLY JSON.
"""

def _plan_with_ai(projdir: Path, tree_full: str, spec_text: str,
                  candidate_roots: List[str], logs: List[str]) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    if not (USE_LLM and _openai_client):
        return None, "llm_unavailable"

    hints = _collect_key_hints(projdir)
    user_prompt = f"""FULL PROJECT TREE (all files, truncated if very large):
<<<TREE
{tree_full[:180000]}
TREE>>>

Candidate project roots (choose one for workdir):
{json.dumps(candidate_roots, ensure_ascii=False, indent=2)}

Key hints:
{json.dumps(hints, ensure_ascii=False, indent=2)}

Assignment (optional context):
<<<SPEC
{spec_text[:4000]}
SPEC>>>

Plan goals:
- Pick the CORRECT workdir from the candidate list (or '.' if top-level).
- Use minimal, safe commands (no apt-get).
- If Maven/Gradle/pytest etc. exist, run tests; otherwise do quick checks (build/compile/lint).
- Keep timeouts modest (<= 240s).
Return STRICT JSON per schema."""
    try:
        text = _chat(user_prompt, PLANNER_SYSTEM)
        data = _extract_json(text)
        plan = _sanitize_plan(data)
        if not plan or not plan.get("services"):
            return None, "empty_plan"
        return plan, None
    except Exception as e:
        logs.append(f"[warn] planner error: {e}")
        return None, str(e)

REFINER_SYSTEM = """You revise a failing run plan. Given the same tree and the previous plan + logs, produce a corrected plan.
- Keep commands minimal and safe (no apt-get).
- If the problem was choosing the wrong directory or language, fix workdir and/or commands.
- Do not add servers that never exit; prefer tests or one-shot checks.
Return ONLY JSON with the same schema as before."""

def _refine_plan_with_ai(projdir: Path, tree_full: str, prior_plan: Dict[str, Any], logs_text: str,
                         candidate_roots: List[str], logs: List[str]) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    if not (USE_LLM and _openai_client):
        return None, "llm_unavailable"

    hints = _collect_key_hints(projdir)
    user_prompt = f"""Previous plan:
{json.dumps(prior_plan, ensure_ascii=False, indent=2)}

FULL PROJECT TREE:
<<<TREE
{tree_full[:180000]}
TREE>>>

Candidate project roots:
{json.dumps(candidate_roots, ensure_ascii=False, indent=2)}

Key hints:
{json.dumps(hints, ensure_ascii=False, indent=2)}

Failure logs (tail):
<<<LOGS
{logs_text[-12000:]}
LOGS>>>

Revise the plan (pick the correct workdir if wrong). Return STRICT JSON."""
    try:
        text = _chat(user_prompt, REFINER_SYSTEM)
        data = _extract_json(text)
        plan = _sanitize_plan(data)
        if not plan or not plan.get("services"):
            return None, "empty_plan"
        return plan, None
    except Exception as e:
        logs.append(f"[warn] refine error: {e}")
        return None, str(e)

def _sanitize_plan(plan: Dict[str, Any]) -> Dict[str, Any]:
    """Ensure plan is well-formed and safe; clamp timeouts; restrict images; default workdir='.'; auto-pick image for mvn/gradle."""
    if not isinstance(plan, dict):
        return {}
    services = plan.get("services")
    if not isinstance(services, list):
        return {}
    out: List[Dict[str, Any]] = []
    for svc in services:
        if not isinstance(svc, dict):
            continue
        name = str(svc.get("name") or f"svc{len(out)+1}")[:64]
        image = str(svc.get("image") or DEFAULT_IMAGE)
        workdir = str(svc.get("workdir") or ".")
        setup = svc.get("setup") or []
        run = svc.get("run") or []
        if not isinstance(setup, list):
            setup = []
        if not isinstance(run, list):
            run = []

        # Auto-pick image if commands clearly require maven/gradle/node
        joined = " && ".join([*map(str, setup), *map(str, run)]).lower()
        if ("mvn" in joined or "maven" in joined):
            mvn_img = next((i for i in ALLOWED_IMAGES if i.startswith("maven:")), None)
            if mvn_img:
                image = mvn_img
        elif "gradle" in joined:
            gr_img = next((i for i in ALLOWED_IMAGES if i.startswith("gradle:")), None)
            if gr_img:
                image = gr_img
        elif any(tok in joined for tok in ("npm ", "pnpm", "yarn", "node ")):
            nd_img = next((i for i in ALLOWED_IMAGES if i.startswith("node:")), None)
            if nd_img:
                image = nd_img

        env = svc.get("env") or {}
        if not isinstance(env, dict):
            env = {}
        network = bool(svc.get("network", False))
        timeout = int(svc.get("timeout") or 180)
        timeout = max(30, min(timeout, 240))

        out.append({
            "name": name,
            "image": image if image in ALLOWED_IMAGES else DEFAULT_IMAGE,
            "workdir": workdir,
            "setup": [str(c) for c in setup][:12],
            "run": [str(c) for c in run][:12] or ["echo 'no-op'"],
            "env": {str(k)[:64]: str(v)[:200] for k, v in env.items()},
            "network": bool(network),
            "timeout": timeout
        })
    return {"services": out}

def _chat(user_content: str, system_content: str) -> str:
    model = os.getenv("OPENAI_MODEL", "gpt-5-mini")
    if hasattr(_openai_client, "chat") and hasattr(_openai_client.chat, "completions"):
        resp = _openai_client.chat.completions.create(
            model=model,
            messages=[{"role": "system", "content": system_content},
                      {"role": "user", "content": user_content}],
        )
        return resp.choices[0].message.content or ""
    else:
        # legacy client
        resp = _openai_client.ChatCompletion.create(
            model=model,
            messages=[{"role": "system", "content": system_content},
                      {"role": "user", "content": user_content}],
        )
        return resp.choices[0].message["content"] or ""

# -----------------------
# Execute plan in Docker
# -----------------------
def _run_services_plan(projdir: Path, plan: Dict[str, Any]) -> Tuple[bool, str]:
    if docker is None:
        return False, "[sandbox] Docker not available."
    services = plan.get("services") or []
    if not services:
        return False, "[plan] No services."

    client = docker.from_env()
    full_logs = []
    ok_any = False

    for svc in services:
        name = svc["name"]
        image = svc["image"]
        network = "bridge" if (ALLOW_NET and svc.get("network")) else "none"
        timeout = int(svc.get("timeout") or 180)

        # Workdir path: resolve against projdir, fallback to best markers
        work_rel = svc.get("workdir") or "."
        svc_root = (projdir / work_rel).resolve()
        if not svc_root.exists() or not svc_root.is_dir():
            best = _best_root_by_markers(projdir)
            if best:
                svc_root = best.resolve()

        volumes = {str(svc_root): {"bind": "/work", "mode": "rw"}}

        setup_cmd = " && ".join([_safe_cmd(c) for c in (svc.get("setup") or [])])
        run_cmd = " && ".join([_safe_cmd(c) for c in (svc.get("run") or [])]) or "echo no-op"

        compound = f"{f'({setup_cmd}) || true; ' if setup_cmd else ''}({run_cmd})"

        env = {k: str(v) for k, v in (svc.get("env") or {}).items()}
        env = {k: v for k, v in env.items() if k.upper() != "PATH"}

        # Professor-visible debug prefix
        debug_head = (f"=== SERVICE {name} ({image}) ===\n"
                      f"Professor: workdir (host) => {svc_root}\n"
                      f"Professor: setup => {setup_cmd or '(none)'}\n"
                      f"Professor: run   => {run_cmd}\n"
                      f"Professor: network => {'on' if network != 'none' else 'off'}, timeout => {timeout}s\n")

        try:
            container = client.containers.run(
                image,
                ['sh', '-lc', compound],
                detach=True,
                working_dir="/work",
                network_mode=network,
                mem_limit="1g",
                nano_cpus=2_000_000_000,
                volumes=volumes,
                environment=env,
            )
        except Exception as e:
            full_logs.append(debug_head + f"[create-error] {e}")
            continue

        try:
            ok = _poll_wait_or_kill(container, timeout)
        finally:
            try:
                clog = container.logs(stdout=True, stderr=True, tail=20000).decode("utf-8", "ignore")
            except Exception:
                clog = ""
            try:
                container.remove(force=True)
            except Exception:
                pass

        full_logs.append(debug_head + clog)
        ok_any = ok_any or ok

    return ok_any, "\n".join(full_logs)

def _poll_wait_or_kill(container, timeout: int) -> bool:
    start = time.time()
    try:
        while True:
            container.reload()
            state = container.attrs.get("State", {})
            status = state.get("Status")
            if status in ("exited", "dead"):
                exit_code = state.get("ExitCode", 1)
                return int(exit_code or 1) == 0
            if time.time() - start > timeout:
                try:
                    container.kill()
                except Exception:
                    pass
                return False
            time.sleep(1.0)
    except Exception:
        try:
            container.kill()
        except Exception:
            pass
        return False

def _safe_cmd(cmd: str) -> str:
    # Light sanitization: prevent backgrounding/daemons; forbid redirection to special files.
    bad = ["&>", ">/dev", "nohup ", "daemon", "systemctl", "service "]
    out = cmd
    for b in bad:
        out = out.replace(b, " ")
    return out.strip()[:2000]

# -----------------------
# Notebook & single-file
# -----------------------
def _handle_notebook(workroot: Path, notebook_path: Path | str, filename: str, spec_text: str, spec_attach: str,
                     logs: List[str], report: Dict[str, Any], sourced: bool = False) -> Dict[str, Any]:
    report["detected_work"] = True
    if not nbformat:
        logs.append("[info] nbformat not installed; static review.")
        text = _safe_read_text(notebook_path, logs)
        return _llm_grade_textual(text, spec_text, spec_attach, {"type": "ipynb-static"}, logs, report)

    run_dir = workroot / "nb_run"
    run_dir.mkdir(exist_ok=True)
    nb_in = Path(notebook_path) if sourced else run_dir / "notebook.ipynb"
    if not sourced:
        shutil.copy2(notebook_path, nb_in)

    try:
        nb = nbformat.read(nb_in, as_version=4)
        nb.cells.append(nbformat.v4.new_code_cell("# Auto-eval\nprint('OK')"))
        nbformat.write(nb, nb_in)
    except Exception as e:
        logs.append(f"[warn] Could not append eval cell: {e}")

    try:
        cmd = [
            "python", "-m", "jupyter", "nbconvert",
            "--to", "notebook", "--execute", str(nb_in),
            "--ExecutePreprocessor.timeout=180", "--output", "executed.ipynb"
        ]
        cp = subprocess.run(cmd, cwd=run_dir if not sourced else nb_in.parent,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=240, text=True)
        out = (cp.stdout or "")[-200000:]
        logs.append(out)
        executed = (run_dir / "executed.ipynb") if not sourced else (nb_in.parent / "executed.ipynb")
        if executed.exists():
            try:
                nb2 = nbformat.read(executed, as_version=4)
                out_text = _notebook_text(nb2)
            except Exception:
                out_text = "(Could not re-open executed notebook)"
        else:
            out_text = "(No executed notebook produced)"
        text_for_llm = f"NOTEBOOK OUTPUT:\n{out_text}\n\nLOG TAIL:\n{out[-4000:]}"
        res = _llm_grade_textual(text_for_llm, spec_text, spec_attach, {"type": "ipynb-exec"}, logs, report)
        if cp.returncode == 0:
            res["grade_pct"] = max(res.get("grade_pct", 0), 80.0)
            res["feedback"] = (res.get("feedback", "") + "\n\nNotebook executed without errors.").strip()
        else:
            res["feedback"] = (res.get("feedback", "") + "\n\nNotebook execution had errors; graded leniently.").strip()
        return res
    except Exception as e:
        logs.append(f"[warn] Notebook execution failed: {e}")
        text = _safe_read_text(nb_in, logs)
        return _llm_grade_textual(text, spec_text, spec_attach, {"type": "ipynb-static"}, logs, report)

def _handle_single_code(workroot: Path, local_path: Path, filename: str, spec_text: str, spec_attach: str,
                        logs: List[str], report: Dict[str, Any]) -> Dict[str, Any]:
    lang = _ext_to_lang(Path(filename).suffix.lower())
    ran, ok, run_logs = _run_single_file_in_sandbox(local_path, lang, timeout=60)
    full = run_logs[-200000:]
    logs.append(full)
    report["sandbox_full_log"] = full
    report["detected_work"] = True

    text_for_llm = f"RUNTIME STDOUT/STDERR (full, truncated):\n{full}"
    res = _llm_grade_textual(text_for_llm, spec_text, spec_attach, {"type": f"single-{lang or 'code'}"}, logs, report)

    # missing_file = ("No such file or directory" in run_logs) or ("can't open file" in run_logs) or ("not found" in run_logs)

    # if ran and ok:
    #     res["grade_pct"] = max(res.get("grade_pct", 0), 75.0)
    #     res["feedback"] = (res.get("feedback", "") + "\n\nProgram ran successfully.").strip()
    # elif missing_file:
    #     report["detected_work"] = False
    #     res["grade_pct"] = 5.0
    #     res["status"] = "failed"
    #     res["feedback"] = "No runnable file was found. Please upload code or a supported archive."
    # elif ran and not ok:
    #     res["grade_pct"] = min(res.get("grade_pct", 100.0), 25.0)
    #     res["feedback"] = (res.get("feedback", "") + "\n\nProgram executed with errors.").strip()
    # else:
    #     res["grade_pct"] = min(res.get("grade_pct", 100.0), 15.0)
    #     res["feedback"] = (res.get("feedback", "") + "\n\nCould not execute; static review only.").strip()
    return res

def _run_single_file_in_sandbox(path: Path, lang: Optional[str], timeout: int = 60) -> Tuple[bool, bool, str]:
    if docker is None:
        return False, False, "[sandbox] Docker not available."
    image = DEFAULT_IMAGE if (lang in (None, "python")) else _image_for_lang(lang)
    if image not in ALLOWED_IMAGES:
        image = DEFAULT_IMAGE
    cmd = _cmd_for_single(path.name, lang)
    client = docker.from_env()
    volumes = {str(path.parent): {"bind": "/work", "mode": "ro"}}
    try:
        c = client.containers.run(
            image, cmd, detach=True, working_dir="/work",
            network_mode="none", mem_limit="512m", nano_cpus=1_000_000_000, volumes=volumes
        )
    except Exception as e:
        return False, False, f"[create-error] {e}"
    try:
        ok = _poll_wait_or_kill(c, timeout)
    finally:
        try:
            out = c.logs(stdout=True, stderr=True, tail=10000).decode("utf-8", "ignore")
        except Exception:
            out = ""
        try:
            c.remove(force=True)
        except Exception:
            pass
    return True, ok, out

def _image_for_lang(lang: Optional[str]) -> str:
    m = {
        "python": "python:3.11", "bash": "python:3.11",
        "node": "node:20", "java": "maven:3.9-eclipse-temurin-17",
        "c": "gcc:13", "cpp": "gcc:13", "go": "golang:1.22",
        "rust": "rust:1.79", "ruby": "ruby:3.3", "php": "php:8.3-cli",
        "dotnet": "mcr.microsoft.com/dotnet/sdk:8.0"
    }
    return m.get(lang or "", DEFAULT_IMAGE)

def _cmd_for_single(fname: str, lang: Optional[str]) -> List[str]:
    p = "/work/" + fname
    if lang in (None, "python") or fname.endswith(".py"):
        return ["python", p]
    if lang == "bash" or fname.endswith(".sh"):
        return ["bash", p]
    if lang == "node" or fname.endswith((".js", ".ts")):
        return ["node", p]
    # default: no direct runner
    return ["sh", "-lc", f"echo 'No direct runner for {fname}' && false"]

# -----------------------
# Inventory & hints
# -----------------------
def _iter_paths(root: Path):
    for p in root.rglob("*"):
        if p.is_file():
            yield p

def _list_files(root: Path) -> List[str]:
    out: List[str] = []
    for p in root.rglob("*"):
        if p.is_file():
            try:
                rel = str(p.relative_to(root))
            except Exception:
                rel = str(p)
            out.append(rel)
        if len(out) >= 20000:  # cap to keep prompt size reasonable
            break
    out.sort()
    return out

def _candidate_roots(root: Path) -> List[str]:
    candidates = set()
    markers = {"manage.py", "pom.xml", "package.json", "pyproject.toml", "build.gradle"}
    for p in root.rglob("*"):
        if p.is_file() and p.name.lower() in markers:
            try:
                candidates.add(str(p.parent.relative_to(root)))
            except Exception:
                pass
    for d in root.rglob("src/main/java"):
        if d.is_dir():
            try:
                candidates.add(str(d.parent.parent.relative_to(root)))  # project root above src
            except Exception:
                pass
    # rank deeper first
    return sorted(candidates, key=lambda s: (len(Path(s).parts), s), reverse=True)

def _best_root_by_markers(root: Path) -> Optional[Path]:
    cands = _candidate_roots(root)
    for rel in cands:
        d = (root / rel)
        if d.exists() and d.is_dir():
            return d
    return None

def _detect_languages(root: Path) -> List[Dict[str, Any]]:
    counts: Dict[str, int] = {}
    for p in _iter_paths(root):
        ext = p.suffix.lower()
        lang = _ext_to_lang(ext)
        if lang:
            counts[lang] = counts.get(lang, 0) + 1
        name = p.name.lower()
        if name in ("pom.xml", "build.gradle", "package.json", "requirements.txt", "pyproject.toml", "makefile", "cmakelists.txt"):
            counts[name] = counts.get(name, 0) + 5
    ranked = sorted(counts.items(), key=lambda x: x[1], reverse=True)
    return [{"language": k, "score": v} for k, v in ranked]

def _ext_to_lang(ext: str) -> Optional[str]:
    m = {
        ".py": "python", ".ipynb": "python", ".sh": "bash", ".js": "node", ".ts": "node",
        ".java": "java", ".c": "c", ".cc": "cpp", ".cpp": "cpp", ".go": "go", ".rs": "rust",
        ".rb": "ruby", ".php": "php", ".cs": "dotnet"
    }
    return m.get(ext)

def _compose_tree_summary(root: Path, files: List[str], max_lines: int = 400) -> str:
    lines = []
    for rel in files[:3000]:
        parts = rel.split("/")
        if len(parts) <= 10:
            lines.append(rel)
        if len(lines) >= max_lines:
            break
    return "\n".join(lines)

def _collect_key_hints(root: Path) -> Dict[str, Any]:
    hints: Dict[str, Any] = {}
    # quick flags
    try:
        hints["has_manage_py"] = any(p.name == "manage.py" for p in _iter_paths(root))
        hints["has_requirements"] = any(p.name == "requirements.txt" for p in _iter_paths(root))
        hints["has_package_json"] = any(p.name == "package.json" for p in _iter_paths(root))
        hints["has_pom_xml"] = any(p.name == "pom.xml" for p in _iter_paths(root))
        hints["has_build_gradle"] = any(p.name.lower() == "build.gradle" for p in _iter_paths(root))
        hints["has_tests_dir"] = (root / "tests").exists() or any("/tests/" in str(p) for p in _iter_paths(root))
        hints["top_dirs"] = sorted({(Path(f).parts[0] if "/" in f else ".") for f in _list_files(root)[:200]})
        hints["requirements_head"] = _read_small_text_if_exists(root, ["requirements.txt"])[:800]
        hints["package_json_head"] = _read_small_text_if_exists(root, ["package.json"])[:800]
        hints["pom_head"] = _read_small_text_if_exists(root, ["pom.xml"])[:800]
    except Exception:
        pass
    return hints

def _read_small_text_if_exists(root: Path, names: List[str]) -> str:
    for n in names:
        p = root / n
        if p.exists() and p.is_file() and p.stat().st_size < 200_000:
            try:
                with open(p, "r", encoding="utf-8", errors="ignore") as f:
                    return f.read(200_000)
            except Exception:
                pass
    return ""

def _compose_grade_context(tree_summary: str, logs_text: str) -> str:
    return f"FILE TREE (truncated):\n{tree_summary}\n\nRUNTIME/BUILD LOGS (truncated):\n{logs_text[-12000:]}"

# -----------------------
# Text extraction / snapshots
# -----------------------
def _extract_text_from_arbitrary_file(django_file, logs: List[str]) -> str:
    name = Path(django_file.name).name.lower()
    mt = mimetypes.guess_type(name)[0] or ""
    tmp_path = _mktempdir(prefix="spec_") / name
    try:
        with django_file.open("rb") as f, open(tmp_path, "wb") as out:
            shutil.copyfileobj(f, out)
    except Exception as e:
        logs.append(f"[warn] Could not save attachment: {e}")
        return ""
    try:
        if name.endswith(".pdf") or "pdf" in mt:
            return _extract_text_from_pdf(tmp_path, logs)
        if name.endswith(".docx") or "word" in mt:
            return _extract_text_from_docx(tmp_path, logs)
        if name.endswith(".txt") or name.endswith(".md") or mt.startswith("text/"):
            return _safe_read_text(tmp_path, logs)
    finally:
        try:
            shutil.rmtree(tmp_path.parent, ignore_errors=True)
        except Exception:
            pass
    return ""

def _extract_text_from_pdf(path: Path | str, logs: List[str]) -> str:
    if not pdfminer_high:
        logs.append("[info] pdfminer not installed; cannot parse PDF.")
        return ""
    try:
        return pdfminer_high.extract_text(str(path)) or ""
    except Exception as e:
        logs.append(f"[warn] PDF parse failed: {e}")
        return ""

def _extract_text_from_docx(path: Path | str, logs: List[str]) -> str:
    if not docx:
        logs.append("[info] python-docx not installed; cannot parse DOCX.")
        return ""
    try:
        d = docx.Document(str(path))
        return "\n".join(p.text for p in d.paragraphs)
    except Exception as e:
        logs.append(f"[warn] DOCX parse failed: {e}")
        return ""

def _safe_read_text(path: Path | str, logs: List[str], limit: int = 200_000) -> str:
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            return f.read(limit)
    except Exception as e:
        logs.append(f"[warn] Text read failed: {e}")
        return ""

def _extract_text_from_image(path: Path | str, logs: List[str]) -> str:
    if PIL is None:
        logs.append("[info] Pillow not installed; cannot read image.")
        return ""
    try:
        from PIL import Image
        img = Image.open(str(path))
        meta = f"(Image size: {img.size}, mode: {img.mode})"
        if pytesseract:
            try:
                txt = pytesseract.image_to_string(img)
                return f"{meta}\n\n{txt}"
            except Exception as e:
                logs.append(f"[warn] OCR failed: {e}")
                return meta
        return meta
    except Exception as e:
        logs.append(f"[warn] Image open failed: {e}")
        return ""

def _best_effort_binary_peek(path: Path | str, logs: List[str], limit: int = 4096) -> str:
    try:
        with open(path, "rb") as f:
            data = f.read(limit)
        return f"(Binary file; first 64 bytes hex): {data[:64].hex()}"
    except Exception as e:
        logs.append(f"[warn] Binary peek failed: {e}")
        return ""

def _gather_text_snapshot(root: Path, logs: List[str], limit_bytes: int = 200_000) -> str:
    chunks: List[str] = []
    total = 0
    for p in _iter_paths(root):
        if p.suffix.lower() in (".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp", ".tiff", ".pdf"):
            continue
        try:
            size = p.stat().st_size
            if size > 50_000:
                continue
            txt = _safe_read_text(p, logs, limit=50_000)
            if txt:
                chunks.append(f"\n--- {p} ---\n{txt}\n")
                total += len(txt)
                if total > limit_bytes:
                    break
        except Exception:
            pass
    return "".join(chunks)

def _notebook_text(nb) -> str:
    out_lines = []
    for cell in nb.cells:
        if getattr(cell, "cell_type", "") == "code":
            for out in cell.get("outputs", []):
                if "text" in out:
                    out_lines.append(out["text"])
                if "data" in out and "text/plain" in out["data"]:
                    out_lines.append(out["data"]["text/plain"])
    return "\n".join(out_lines)[:200000]

# -----------------------
# LLM grading
# -----------------------
LENIENT_SYSTEM = """You are a fair, assignment-agnostic grader.
Be lenient on minor issues; focus on core correctness and plausible effort.
Return clear, constructive feedback."""

def _llm_grade_textual(student_text: str, spec_text: str, spec_attach: str, context: Dict[str, Any],
                       logs: List[str], report: Dict[str, Any]) -> Dict[str, Any]:
    length = len(student_text or "")
    detected_work = length > 0 or bool(spec_attach)
    report["detected_work"] = report.get("detected_work", False) or detected_work

    if USE_LLM and _openai_client:
        try:
            prompt = f"""
Context: {json.dumps(context, ensure_ascii=False)}
Assignment description:
<<<
{spec_text}
>>>

Attachment (first 4000 chars):
<<<
{spec_attach[:4000]}
>>>

Submission artifacts (logs/text snapshot; truncated):
<<<
{(student_text or '')[:12000]}
>>>

Tasks:
1) Briefly summarize what was attempted and whether it meets core requirements.
2) Give 3–6 specific, constructive suggestions (be kind).
3) Output a LENIENT numeric grade 0–100 (float). Penalize only major issues (no runnable code, severe errors, or no substance).
Return strict JSON: {{"summary": "str", "suggestions": ["str", "..."], "grade_pct": 85.0}}
"""
            # NOTE: The example above uses JSON braces but is a plain string; it's safe. We avoid printing this into templates directly.
            text = _chat(prompt, LENIENT_SYSTEM)
            data = _extract_json(text)
            grade = float(data.get("grade_pct", 70.0))
            suggestions = data.get("suggestions", [])
            if isinstance(suggestions, list):
                sugg_text = "\n- ".join(str(s) for s in suggestions)
            else:
                sugg_text = str(suggestions)
            feedback = f"{data.get('summary','')}\n\nSuggestions:\n- {sugg_text}" if sugg_text else str(data.get("summary",""))
            return _final("done" if detected_work else "partial", _clamp(grade), feedback, report, "\n".join(logs), time.time())
        except Exception as e:
            report["llm_used"] = False
            report["llm_error"] = str(e)
            logs.append(f"[warn] LLM grade failed: {e}")

    # Heuristic fallback
    if not detected_work:
        return _final("failed", 5.0, "No meaningful content detected in submission.", report, "\n".join(logs), time.time())
    base = 70.0
    if length > 2000:
        base += 10
    feedback = ("Automated review (no LLM):\n"
                "- We detected content and attempted to match it to the assignment.\n"
                "- This is an estimate; final grade may be adjusted by your professor.")
    return _final("partial", _clamp(base), feedback, report, "\n".join(logs), time.time())

def _extract_json(text: str) -> Dict[str, Any]:
    m = re.search(r"\{.*\}", text, flags=re.S)
    if not m:
        return {}
    try:
        return json.loads(m.group(0))
    except Exception:
        return {}

def _clamp(x: float, a: float = 0.0, b: float = 100.0) -> float:
    return max(a, min(b, x))

# -----------------------
# Fallback planner (generic, not assignment-specific)
# -----------------------
def _fallback_plan(projdir: Path) -> Dict[str, Any]:
    files = set(_list_files(projdir))

    def has(name: str) -> bool:
        return any(Path(f).name.lower() == name for f in files)

    def has_any(names: List[str]) -> bool:
        return any(has(n) for n in names)

    services: List[Dict[str, Any]] = []

    # Python/Django generic
    if has("manage.py") or has("requirements.txt") or has("pyproject.toml"):
        services.append({
            "name": "python-checks",
            "image": DEFAULT_IMAGE,
            "workdir": ".",
            "setup": [
                "python -m pip install -U pip wheel setuptools || true",
                "[ -f requirements.txt ] && pip install -r requirements.txt || true",
                "[ -f pyproject.toml ] && pip install . || true"
            ],
            "run": [
                "[ -f manage.py ] && python manage.py check || true",
                "pytest -q || true",
                "python -m unittest -q || true",
                "echo 'done'"
            ],
            "network": False,
            "timeout": 180
        })
    # Node generic
    elif has("package.json"):
        img = "node:20" if "node:20" in ALLOWED_IMAGES else DEFAULT_IMAGE
        services.append({
            "name": "node-checks",
            "image": img,
            "workdir": ".",
            "setup": ["npm ci --silent || npm i --silent || true"],
            "run":   ["npm test --silent || npm run build --silent || true", "echo 'done'"],
            "network": False,
            "timeout": 180
        })
    # Maven/Gradle generic
    elif has("pom.xml") or has("build.gradle"):
        img = next((i for i in ALLOWED_IMAGES if i.startswith("maven:")), DEFAULT_IMAGE)
        # Prefer tests if present
        run_cmds = [
            "mvn -B -q -DskipTests=false test || mvn -B -q -DskipTests package || true"
        ]
        services.append({
            "name": "java-tests",
            "image": img,
            "workdir": ".",
            "run": run_cmds,
            "network": False,
            "timeout": 180
        })
    else:
        services.append({
            "name": "static-snapshot",
            "image": DEFAULT_IMAGE,
            "workdir": ".",
            "run": ["echo 'no build system detected'"],
            "network": False,
            "timeout": 60
        })

    return {"services": services}

# -----------------------
# Misc utils
# -----------------------
def _is_archive(name: str) -> bool:
    name = name.lower()
    return name.endswith(".zip") or name.endswith(".tar.gz") or name.endswith(".tgz") or name.endswith(".tar")

def _looks_like_code(name: str) -> bool:
    exts = (".py", ".sh", ".js", ".ts", ".java", ".c", ".cc", ".cpp", ".go", ".rs", ".rb", ".php", ".cs")
    return name.lower().endswith(exts)

def _looks_like_image(name: str, mt: str) -> bool:
    return any(name.lower().endswith(e) for e in (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tiff", ".svg")) or (mt and mt.startswith("image/"))

def _mktempdir(prefix: str = "autograde_") -> Path:
    base = Path(os.getenv("GRADER_SHARED_DIR", "/grader-shared"))
    try:
        base.mkdir(parents=True, exist_ok=True)
        if os.access(base, os.W_OK):
            return Path(tempfile.mkdtemp(prefix=prefix, dir=str(base)))
    except Exception:
        pass
    return Path(tempfile.mkdtemp(prefix=prefix))

def _final(status: str, grade: float, feedback: str, report: Dict[str, Any], logs_joined: str, start: float) -> Dict[str, Any]:
    logs_text = (logs_joined or "")[-200000:]
    report.setdefault("sandbox_full_log", logs_text)
    return {
        "status": status,
        "grade_pct": _clamp(grade),
        "feedback": (feedback or "").strip(),
        "report": report,
        "logs": logs_text,
        "finished_at": timezone.now().isoformat(),
        "elapsed_s": max(0.0, time.time() - start),
    }