# assignments/autograder.py
"""
Universal, lenient autograder that accepts ANY file type:

- Archives (.zip, .tar.gz, .tgz): unpack, detect languages, try to build/run,
  else static/LLM review.
- Notebooks (.ipynb / Google Colab): execute offline with nbconvert/nbconvert,
  append gentle evaluation cell, grade leniently.
- Single code files (.py, .sh, .js, .java, .c/.cpp, etc.): run inside Docker
  (if available) or optional local runner; capture output and grade.
- PDFs / DOCX / TXT / MD: extract text and review with LLM.
- Images: optional OCR (pytesseract) or metadata-based review.

It fails softly: if a capability is missing, it falls back to text review.

Environment flags:
- AUTOGRADER_DISABLE_DOCKER=1       -> skip docker; try local (unsafe)
- AUTOGRADER_ENABLE_LOCAL_EXEC=1    -> allow local subprocess fallback
- AUTOGRADER_USE_LLM=1              -> enable OpenAI-based feedback/grade if OPENAI_API_KEY is set
- OPENAI_MODEL                      -> model name; default "gpt-5-mini"
- AUTOGRADER_DOCKER_IMAGE_PY        -> override default Python image
- AUTOGRADER_DOCKER_IMAGE_DS        -> override data-science image (notebook runner)
Timeouts are implemented via polling + kill, since docker SDK doesn't accept
`timeout=` on containers.run.
"""

from __future__ import annotations

import io
import os
import re
import sys
import json
import time
import shutil
import tarfile
import zipfile
import tempfile
import mimetypes
import subprocess
import importlib
from pathlib import Path
from typing import Dict, Any, List, Optional

from django.utils import timezone

# Optional heavy deps guarded by try/except
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

docx = _try_import("docx")  # python-docx
PIL = _try_import("PIL")
pytesseract = _try_import("pytesseract")
docker = _try_import("docker")

# --- LLM (OpenAI) ---
USE_LLM = bool(os.getenv("AUTOGRADER_USE_LLM", "0") == "1") and bool(os.getenv("OPENAI_API_KEY"))
_openai_client = None
if USE_LLM:
    try:
        openai_mod = importlib.import_module("openai")
        OpenAI = getattr(openai_mod, "OpenAI", None)
        if OpenAI:
            _openai_client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
    except Exception:
        _openai_client = None


# -----------------------
# Public API
# -----------------------
def grade_submission(assignment, submission) -> Dict[str, Any]:
    """
    Entry point. Downloads the submission file to a temp dir, decides the pathway,
    runs best-effort checks, and returns a dict:
      {
        "status": "done" | "partial" | "failed",
        "grade_pct": float,
        "feedback": str,
        "report": {...},     # machine-readable details
        "logs": str          # raw logs for debugging
      }
    Always be lenient in scoring.
    """
    start = time.time()
    logs: List[str] = []
    report: Dict[str, Any] = {"steps": []}

    # Pull assignment description (and attached assignment file text if feasible)
    spec_text = (getattr(assignment, "description", "") or "").strip()
    spec_attachment_text = ""
    try:
        a_file = getattr(assignment, "file", None)
        if a_file and a_file.name:
            spec_attachment_text = _extract_text_from_arbitrary_file(a_file, logs)
    except Exception as e:
        logs.append(f"[warn] Failed reading assignment attachment: {e}")

    # Download submission into temp file
    tmp_dir = _mktempdir(prefix="autograde_")
    local_path = tmp_dir / "submission.bin"
    try:
        f = submission.file.open("rb")  # storage-agnostic file object
        with open(local_path, "wb") as out:
            shutil.copyfileobj(f, out)
        f.close()
    except Exception as e:
        logs.append(f"[error] Could not read submission from storage: {e}")
        return _final("failed", 0.0, "Could not read your file from storage.", report, "\n".join(logs), start)

    # Decide pathway
    name = Path(submission.file.name).name.lower()
    mimetype = mimetypes.guess_type(name)[0] or "application/octet-stream"
    report["filename"] = name
    report["mimetype"] = mimetype

    try:
        if _is_archive(name):
            res = _handle_archive(tmp_dir, local_path, name, spec_text, spec_attachment_text, logs, report)

        elif name.endswith(".ipynb"):
            res = _handle_notebook(tmp_dir, local_path, name, spec_text, spec_attachment_text, logs, report)

        elif _looks_like_code(name):
            res = _handle_single_code(tmp_dir, local_path, name, spec_text, spec_attachment_text, logs, report)

        elif name.endswith(".pdf") or "pdf" in mimetype:
            text = _extract_text_from_pdf(local_path, logs)
            res = _llm_grade_textual(text, spec_text, spec_attachment_text, context={"type": "pdf"}, logs=logs, report=report)

        elif name.endswith(".docx") or "word" in mimetype:
            text = _extract_text_from_docx(local_path, logs)
            res = _llm_grade_textual(text, spec_text, spec_attachment_text, context={"type": "docx"}, logs=logs, report=report)

        elif name.endswith(".txt") or name.endswith(".md") or "text" in mimetype:
            text = _safe_read_text(local_path, logs)
            res = _llm_grade_textual(text, spec_text, spec_attachment_text, context={"type": "text"}, logs=logs, report=report)

        elif _looks_like_image(name, mimetype):
            text = _extract_text_from_image(local_path, logs)
            res = _llm_grade_textual(text, spec_text, spec_attachment_text, context={"type": "image"}, logs=logs, report=report)

        else:
            # Unknown: try to peek a bit and LLM summarize
            text = _best_effort_binary_peek(local_path, logs)
            res = _llm_grade_textual(text, spec_text, spec_attachment_text, context={"type": "binary"}, logs=logs, report=report)

    except Exception as e:
        logs.append(f"[error] Pipeline crashed: {e}")
        res = _final("failed", 50.0, "We could not fully analyze your file, but awarding generous partial credit. Please check feedback.", report, "\n".join(logs), start)

    # Clean
    try:
        shutil.rmtree(tmp_dir, ignore_errors=True)
    except Exception:
        pass

    # Ensure lenient minimums if non-empty work detected
    if res["status"] in ("done", "partial") and res.get("grade_pct", 0) < 40 and report.get("detected_work", False):
        res["logs"] += "\n[policy] Leniency floor applied because work was detected."
        res["grade_pct"] = max(res.get("grade_pct", 0), 40.0)

    return res


def apply_result_to_submission(submission, result: Dict[str, Any]) -> None:
    """Persist the result to the submission model, if fields exist."""
    if hasattr(submission, "grade_pct"):
        # Only set grade if present (Celery may decide to hold)
        grade = result.get("grade_pct", None)
        submission.grade_pct = float(grade) if grade is not None else None
    if hasattr(submission, "ai_feedback"):
        submission.ai_feedback = str(result.get("feedback", ""))
    if hasattr(submission, "autograde_status"):
        submission.autograde_status = result.get("status", "done")
    if hasattr(submission, "autograde_report"):
        try:
            # If model field is JSONField, we can assign dict; if TextField use json.dumps.
            submission.autograde_report = result.get("report", {})
        except Exception:
            submission.autograde_report = {}
    if hasattr(submission, "runner_logs"):
        submission.runner_logs = str(result.get("logs", ""))


# -----------------------
# Helpers: main branches
# -----------------------
def _is_archive(name: str) -> bool:
    name = name.lower()
    return name.endswith(".zip") or name.endswith(".tar.gz") or name.endswith(".tgz") or name.endswith(".tar")


def _looks_like_code(name: str) -> bool:
    exts = (".py", ".sh", ".js", ".ts", ".java", ".c", ".cc", ".cpp", ".go", ".rs", ".rb", ".php", ".cs")
    return name.lower().endswith(exts)


def _looks_like_image(name: str, mt: str) -> bool:
    return any(name.lower().endswith(e) for e in (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tiff", ".svg")) or (mt and mt.startswith("image/"))


def _handle_archive(tmp_dir: Path, local_path: Path, filename: str, spec_text: str, spec_attach: str,
                    logs: List[str], report: Dict[str, Any]) -> Dict[str, Any]:
    workdir = tmp_dir / "work"
    workdir.mkdir(exist_ok=True)
    report["detected_work"] = True

    # Extract
    try:
        if filename.endswith(".zip"):
            with zipfile.ZipFile(local_path, "r") as zf:
                zf.extractall(workdir)
        else:
            with tarfile.open(local_path, "r:*") as tf:
                tf.extractall(workdir)
        logs.append(f"[ok] Archive extracted into {workdir}")
    except Exception as e:
        logs.append(f"[error] Could not extract archive: {e}")
        text = _best_effort_binary_peek(local_path, logs)
        return _llm_grade_textual(text, spec_text, spec_attach, context={"type": "archive-corrupt"}, logs=logs, report=report)

    # Inventory + language detect + special notebook path
    files = _list_files(workdir)
    report["file_tree"] = files[:3000]  # cut for size
    langs = _detect_languages(workdir)
    report["languages"] = langs

    # Notebook first (common for Colab zips)
    nb_files = [p for p in _iter_paths(workdir) if p.suffix == ".ipynb"]
    if nb_files and nbformat:
        best_nb = nb_files[0]
        return _handle_notebook(tmp_dir, best_nb, best_nb.name, spec_text, spec_attach, logs, report, sourced=True)

    # Try runnable build/run for dominant lang
    if langs:
        primary = langs[0]["language"]
        return _build_and_run_project(workdir, primary, spec_text, spec_attach, logs, report)

    # If no code detected, do textual analysis of everything
    text = _gather_text_snapshot(workdir, logs, limit_bytes=200_000)
    return _llm_grade_textual(text, spec_text, spec_attach, context={"type": "archive-mixed"}, logs=logs, report=report)


def _handle_notebook(tmp_dir: Path, notebook_path: Path | str, filename: str, spec_text: str, spec_attach: str,
                     logs: List[str], report: Dict[str, Any], sourced: bool=False) -> Dict[str, Any]:
    """
    Execute a single notebook offline. If `notebook_path` is a Path in a workdir (sourced=True),
    use it directly; otherwise copy the uploaded .ipynb into tmp_dir and run there.
    """
    report["detected_work"] = True
    if not nbformat:
        logs.append("[info] nbformat/nbconvert not installed; falling back to reading notebook JSON only.")
        text = _safe_read_text(notebook_path, logs)
        return _llm_grade_textual(text, spec_text, spec_attach, context={"type": "ipynb-static"}, logs=logs, report=report)

    # Prepare runner dir
    run_dir = tmp_dir / "nb_run"
    run_dir.mkdir(exist_ok=True)
    nb_in = Path(notebook_path) if sourced else run_dir / "notebook.ipynb"
    if not sourced:
        shutil.copy2(notebook_path, nb_in)

    # Append gentle eval cell
    try:
        nb = nbformat.read(nb_in, as_version=4)
        nb.cells.append(nbformat.v4.new_code_cell(
            "# Auto-eval cell (appended by autograder)\nprint('OK')"
        ))
        nbformat.write(nb, nb_in)
    except Exception as e:
        logs.append(f"[warn] Could not append eval cell: {e}")

    # Execute with nbconvert (offline)
    try:
        cmd = [
            sys.executable, "-m", "jupyter", "nbconvert",
            "--to", "notebook",
            "--execute", str(nb_in),
            "--ExecutePreprocessor.timeout=180",
            "--output", "executed.ipynb"
        ]
        cp = subprocess.run(cmd, cwd=run_dir if not sourced else nb_in.parent,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=240, text=True)
        logs.append(cp.stdout[-4000:])

        # Load executed output to collect text content
        executed = (run_dir / "executed.ipynb") if not sourced else (nb_in.parent / "executed.ipynb")
        if executed.exists():
            try:
                nb2 = nbformat.read(executed, as_version=4)
                out_text = _notebook_text(nb2)
            except Exception:
                out_text = "(Could not re-open executed notebook; using logs only.)"
        else:
            out_text = "(No executed notebook produced; using logs only.)"

        # LLM lenient scoring
        text_for_llm = f"NOTEBOOK OUTPUT/LOGS:\n{out_text}\n\nRUN LOG TAIL:\n{logs[-1] if logs else ''}"
        res = _llm_grade_textual(text_for_llm, spec_text, spec_attach, context={"type": "ipynb-exec"}, logs=logs, report=report)
        # Bonus if ran to completion
        if cp.returncode == 0:
            res["grade_pct"] = min(100.0, res.get("grade_pct", 0) + 10)
            res["feedback"] = (res.get("feedback", "") + "\n\nBonus: notebook executed without errors.").strip()
        else:
            res["feedback"] = (res.get("feedback", "") + "\n\nNote: execution had errors; we still graded leniently.").strip()
        return res
    except Exception as e:
        logs.append(f"[warn] Notebook execution failed: {e}")
        text = _safe_read_text(nb_in, logs)
        return _llm_grade_textual(text, spec_text, spec_attach, context={"type": "ipynb-static"}, logs=logs, report=report)


def _handle_single_code(tmp_dir: Path, local_path: Path, filename: str, spec_text: str, spec_attach: str,
                        logs: List[str], report: Dict[str, Any]) -> Dict[str, Any]:
    report["detected_work"] = True
    lang = _ext_to_lang(Path(filename).suffix.lower())
    # Try to run with Docker
    ran, ok, run_logs = _run_single_file_in_sandbox(local_path, lang, timeout=60)
    logs.append(run_logs[-4000:])
    context = {"type": f"single-{lang or 'code'}"}
    text_for_llm = f"RUNTIME OUTPUT TAIL:\n{run_logs[-2000:]}"
    res = _llm_grade_textual(text_for_llm, spec_text, spec_attach, context=context, logs=logs, report=report)
    if ran and ok:
        res["grade_pct"] = max(res.get("grade_pct", 0), 75.0)
        res["feedback"] = (res.get("feedback", "") + "\n\nProgram ran successfully.").strip()
    elif ran and not ok:
        res["feedback"] = (res.get("feedback", "") + "\n\nProgram executed with errors; graded leniently.").strip()
        res["grade_pct"] = max(res.get("grade_pct", 0), 60.0)
    else:
        res["feedback"] = (res.get("feedback", "") + "\n\nCould not execute; static/LLM review only.").strip()
        res["grade_pct"] = max(res.get("grade_pct", 0), 55.0)
    return res


def _build_and_run_project(workdir: Path, lang: str, spec_text: str, spec_attach: str,
                           logs: List[str], report: Dict[str, Any]) -> Dict[str, Any]:
    """
    Minimal heuristics per language. We do "best-effort" build/run, then LLM-grade logs.
    """
    ran, ok, run_logs = _run_project_in_sandbox(workdir, lang, timeout=180)
    logs.append(run_logs[-4000:])
    context = {"type": f"project-{lang}"}
    text_for_llm = f"BUILD/RUN LOGS TAIL:\n{run_logs[-2500:]}\n\nFILE TREE (first 3000 lines):\n" + "\n".join(report.get("file_tree", [])[:3000])
    res = _llm_grade_textual(text_for_llm, spec_text, spec_attach, context=context, logs=logs, report=report)
    if ran and ok:
        res["grade_pct"] = max(res.get("grade_pct", 0), 80.0)
        res["feedback"] = (res.get("feedback", "") + "\n\nProject built and ran.").strip()
    elif ran and not ok:
        res["feedback"] = (res.get("feedback", "") + "\n\nBuild/run issues; graded leniently.").strip()
        res["grade_pct"] = max(res.get("grade_pct", 0), 65.0)
    else:
        res["feedback"] = (res.get("feedback", "") + "\n\nCould not build/run; static review only.").strip()
        res["grade_pct"] = max(res.get("grade_pct", 0), 55.0)
    return res


# -----------------------
# Utilities: language detect & runners
# -----------------------
def _iter_paths(root: Path):
    for p in root.rglob("*"):
        if p.is_file():
            yield p


def _list_files(root: Path) -> List[str]:
    paths = []
    for p in _iter_paths(root):
        try:
            rel = str(p.relative_to(root))
        except Exception:
            rel = str(p)
        paths.append(rel)
        if len(paths) > 5000:
            break
    paths.sort()
    return paths


def _detect_languages(root: Path) -> List[Dict[str, Any]]:
    counts = {}
    for p in _iter_paths(root):
        ext = p.suffix.lower()
        lang = _ext_to_lang(ext)
        if lang:
            counts[lang] = counts.get(lang, 0) + 1
        # build files
        name = p.name.lower()
        if name in ("pom.xml", "build.gradle", "package.json", "requirements.txt", "pyproject.toml", "makefile", "cmakelists.txt"):
            counts[name] = counts.get(name, 0) + 5
    ranked = sorted(counts.items(), key=lambda x: x[1], reverse=True)
    return [{"language": k, "score": v} for k, v in ranked]


def _ext_to_lang(ext: str) -> Optional[str]:
    mapping = {
        ".py": "python", ".ipynb": "python", ".sh": "bash", ".js": "node",
        ".ts": "node", ".java": "java", ".c": "c", ".cc": "cpp", ".cpp": "cpp",
        ".go": "go", ".rs": "rust", ".rb": "ruby", ".php": "php", ".cs": "dotnet"
    }
    return mapping.get(ext)


def _docker_available() -> bool:
    if os.getenv("AUTOGRADER_DISABLE_DOCKER") == "1":
        return False
    return docker is not None


def _run_single_file_in_sandbox(path: Path, lang: Optional[str], timeout: int = 60):
    """
    Try to run a single source file. Docker first; local fallback opt-in.
    Returns (ran: bool, ok: bool, logs: str).
    """
    logs = ""
    ran = False
    ok = False
    try:
        if _docker_available():
            ran, ok, logs = _docker_run_single(path, lang, timeout)
        elif os.getenv("AUTOGRADER_ENABLE_LOCAL_EXEC") == "1":
            ran, ok, logs = _local_run_single(path, lang, timeout)
        else:
            logs = "[safe] Execution disabled (no docker and local exec not enabled)."
    except Exception as e:
        logs += f"\n[error] Runner crashed: {e}"
    return ran, ok, logs


def _run_project_in_sandbox(workdir: Path, lang: str, timeout: int = 180):
    """
    Best-effort project build/run. Docker preferred.
    """
    logs = ""
    ran = False
    ok = False
    try:
        if _docker_available():
            ran, ok, logs = _docker_run_project(workdir, lang, timeout)
        elif os.getenv("AUTOGRADER_ENABLE_LOCAL_EXEC") == "1":
            ran, ok, logs = _local_run_project(workdir, lang, timeout)
        else:
            logs = "[safe] Execution disabled (no docker and local exec not enabled)."
    except Exception as e:
        logs += f"\n[error] Runner crashed: {e}"
    return ran, ok, logs


# ---- Docker runners (fixed timeouts; no invalid kwargs) ----
def _poll_wait_or_kill(container, timeout: int) -> bool:
    """
    Poll container status up to `timeout` seconds; kill on timeout.
    Return True if exit code == 0, else False.
    """
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


def _docker_run_single(path: Path, lang: Optional[str], timeout: int):
    client = docker.from_env()
    volumes = {str(path.parent): {"bind": "/work", "mode": "ro"}}
    cmd = _lang_cmd_single("/work/" + path.name, lang)
    image = _choose_image_for_lang(lang)

    container = client.containers.run(
        image,
        cmd,
        detach=True,
        working_dir="/work",
        network_mode="none",
        mem_limit="512m",
        nano_cpus=1_000_000_000,
        volumes=volumes,
    )
    try:
        ok = _poll_wait_or_kill(container, timeout)
    finally:
        try:
            logs = container.logs(stdout=True, stderr=True, tail=10000).decode("utf-8", "ignore")
        except Exception:
            logs = ""
        try:
            container.remove(force=True)
        except Exception:
            pass

    return True, ok, logs


def _docker_run_project(workdir: Path, lang: str, timeout: int):
    client = docker.from_env()
    volumes = {str(workdir): {"bind": "/work", "mode": "rw"}}
    cmd = _lang_cmd_project(lang)
    image = _choose_image_for_lang(lang, project=True)

    container = client.containers.run(
        image,
        cmd,
        detach=True,
        working_dir="/work",
        network_mode="none",
        mem_limit="1g",
        nano_cpus=2_000_000_000,
        volumes=volumes,
    )
    try:
        ok = _poll_wait_or_kill(container, timeout)
    finally:
        try:
            logs = container.logs(stdout=True, stderr=True, tail=20000).decode("utf-8", "ignore")
        except Exception:
            logs = ""
        try:
            container.remove(force=True)
        except Exception:
            pass

    return True, ok, logs


def _choose_image_for_lang(lang: Optional[str], project: bool=False) -> str:
    if lang in ("python", None):
        if project:
            return os.getenv("AUTOGRADER_DOCKER_IMAGE_DS", "python:3.11")
        return os.getenv("AUTOGRADER_DOCKER_IMAGE_PY", "python:3.11")
    if lang == "node":
        return "node:20"
    if lang == "java":
        return "maven:3.9-eclipse-temurin-17"
    if lang in ("c", "cpp"):
        return "gcc:13"
    if lang == "go":
        return "golang:1.22"
    if lang == "rust":
        return "rust:1.79"
    if lang == "ruby":
        return "ruby:3.3"
    if lang == "php":
        return "php:8.3-cli"
    if lang == "dotnet":
        return "mcr.microsoft.com/dotnet/sdk:8.0"
    return "python:3.11"


def _lang_cmd_single(file_in_container: str, lang: Optional[str]) -> List[str]:
    if lang == "python" or file_in_container.endswith(".py"):
        return ["python", file_in_container]
    if lang == "bash" or file_in_container.endswith(".sh"):
        return ["bash", file_in_container]
    if lang == "node" or file_in_container.endswith((".js", ".ts")):
        # naive ts via ts-node could be added; here assume JS
        return ["node", file_in_container]
    # others could be compiled first; keep simple:
    return ["sh", "-lc", f"echo 'No direct runner for {file_in_container}' && false"]


def _lang_cmd_project(lang: str) -> List[str]:
    if lang == "python":
        return ["sh", "-lc", "python -V && if [ -f requirements.txt ]; then pip install -q -r requirements.txt; fi && python -m pytest -q || python main.py || true"]
    if lang == "node":
        return ["sh", "-lc", "node -v && if [ -f package.json ]; then npm ci --silent || npm i --silent; npm test --silent || npm run start --silent || node . || true; else node . || true; fi"]
    if lang == "java":
        return ["sh", "-lc", "if [ -f pom.xml ]; then mvn -q -e -DskipTests package && java -jar target/*.jar || true; else find -name '*.java' -print -exec javac {} + && java Main || true; fi"]
    if lang in ("c", "cpp"):
        return ["sh", "-lc", "if [ -f Makefile ]; then make -s && ./a.out || true; else find -name '*.c' -o -name '*.cpp' | xargs -r gcc -std=c11 -O2 -o app && ./app || true; fi"]
    if lang == "go":
        return ["sh", "-lc", "go version && go test ./... || go run . || true"]
    if lang == "rust":
        return ["sh", "-lc", "cargo test -q || cargo run -q || true"]
    if lang == "dotnet":
        return ["sh", "-lc", "dotnet --info && dotnet build -clp:ErrorsOnly || true && dotnet test -l:trx || true"]
    if lang == "ruby":
        return ["sh", "-lc", "ruby -v && ruby main.rb || true"]
    if lang == "php":
        return ["sh", "-lc", "php -v && php index.php || true"]
    return ["sh", "-lc", "echo 'No project runner for this language'; true"]


# ---- Local runners (opt-in, unsafe) ----
def _local_run_single(path: Path, lang: Optional[str], timeout: int):
    cmd = _lang_cmd_single(str(path), lang)
    cp = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=timeout, text=True)
    return True, (cp.returncode == 0), cp.stdout


def _local_run_project(workdir: Path, lang: str, timeout: int):
    cmd = _lang_cmd_project(lang)
    cp = subprocess.run(cmd, cwd=workdir, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=timeout, text=True)
    return True, (cp.returncode == 0), cp.stdout


# -----------------------
# Text extraction
# -----------------------
def _extract_text_from_arbitrary_file(django_file, logs: List[str]) -> str:
    """
    Reads a Django FileField without touching local fs (for small texts) if possible.
    Otherwise downloads to temp and routes via helpers.
    """
    name = Path(django_file.name).name.lower()
    mt = mimetypes.guess_type(name)[0] or ""
    try:
        django_file.open("rb").close()  # probe availability
    except Exception as e:
        logs.append(f"[warn] Could not peek attachment: {e}")
        return ""

    tmp_path = _mktempdir(prefix="spec_") / name
    try:
        f = django_file.open("rb")
        with open(tmp_path, "wb") as out:
            shutil.copyfileobj(f, out)
        f.close()
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
        text = pdfminer_high.extract_text(str(path))
        return text or ""
    except Exception as e:
        logs.append(f"[warn] PDF parse failed: {e}")
        return ""


def _extract_text_from_docx(path: Path | str, logs: List[str]) -> str:
    if not docx:
        logs.append("[info] python-docx not installed; cannot parse DOCX.")
        return ""
    try:
        doc = docx.Document(str(path))
        return "\n".join(p.text for p in doc.paragraphs)
    except Exception as e:
        logs.append(f"[warn] DOCX parse failed: {e}")
        return ""


def _safe_read_text(path: Path | str, logs: List[str], limit: int = 200_000) -> str:
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            data = f.read(limit)
        return data
    except Exception as e:
        logs.append(f"[warn] Text read failed: {e}")
        return ""


def _extract_text_from_image(path: Path | str, logs: List[str]) -> str:
    if PIL is None:
        logs.append("[info] Pillow not installed; cannot read image OCR.")
        return ""
    try:
        from PIL import Image
        img = Image.open(str(path))
        meta = f"(Image size: {img.size}, mode: {img.mode})"
        if pytesseract:
            try:
                text = pytesseract.image_to_string(img)
                return f"{meta}\n\n{text}"
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
        hexs = data[:64].hex()
        return f"(Binary file; first 64 bytes hex): {hexs}"
    except Exception as e:
        logs.append(f"[warn] Binary peek failed: {e}")
        return ""


def _gather_text_snapshot(root: Path, logs: List[str], limit_bytes: int = 200_000) -> str:
    """
    Concatenate small text files to feed LLM; skip big binaries.
    """
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
        if cell.cell_type == "code":
            for out in cell.get("outputs", []):
                if "text" in out:
                    out_lines.append(out["text"])
                if "data" in out and "text/plain" in out["data"]:
                    out_lines.append(out["data"]["text/plain"])
    return "\n".join(out_lines)[:200_000]


# -----------------------
# LLM grading (lenient)
# -----------------------
LENIENT_SYSTEM = """You are a gentle, fair grader for programming assignments.
Be LENIENT. Small mistakes (formatting, naming, minor inefficiencies) should only reduce the grade slightly.
Focus on core correctness and whether the submission plausibly satisfies the assignment.
Return clear, constructive, encouraging feedback.
"""

def _llm_grade_textual(student_text: str, spec_text: str, spec_attach: str, context: Dict[str, Any],
                       logs: List[str], report: Dict[str, Any]) -> Dict[str, Any]:
    """
    Grade based on available text: code logs, extracted text, readme, notebook output, etc.
    If no LLM available, use a simple heuristic that yields a kind, partial grade.
    """
    # Simple signals
    length = len(student_text or "")
    detected_work = length > 0 or bool(spec_attach)
    report["detected_work"] = report.get("detected_work", False) or detected_work

    if _openai_client and USE_LLM:
        try:
            report["llm_used"] = True
            prompt = f"""
Grading context: {json.dumps(context)}
Assignment description (may be vague): 
<<<
{spec_text}
>>>

Additional assignment attachment (if any):
<<<
{spec_attach[:4000]}
>>>

Student submission content / logs / text snapshot:
<<<
{(student_text or '')[:12000]}
>>>

Your tasks:
1) Briefly summarize what the student attempted and whether it meets the core requirements.
2) List 3–6 specific, constructive suggestions for improvement (be kind).
3) Give a LENIENT numeric grade 0–100 (float), prioritizing core correctness; small issues should have minimal impact.
Return strict JSON with keys: summary, suggestions (array), grade_pct (float).
"""
            resp = _openai_client.chat.completions.create(
                model=_choose_model_for_context(context),
                messages=[
                    {"role": "system", "content": LENIENT_SYSTEM},
                    {"role": "user", "content": prompt}
                ],
                # IMPORTANT: omit temperature to satisfy models that only support the default
            )
            text = resp.choices[0].message.content or ""
            data = _extract_json(text)
            grade = float(data.get("grade_pct", 75.0))
            feedback = f"{data.get('summary','')}\n\nSuggestions:\n- " + "\n- ".join(data.get("suggestions", []))
            return _final("done", _clamp(grade), feedback, report, "\n".join(logs), time.time())
        except Exception as e:
            report["llm_used"] = False
            report["llm_error"] = str(e)
            logs.append(f"[warn] LLM call failed: {e}")

    # Fallback heuristic
    base = 70.0 if detected_work else 10.0
    if length > 2000:
        base += 10
    feedback = (
        "Automated lenient review:\n"
        "- We detected content and attempted to match it to the assignment.\n"
        "- This is a helpful estimate; final grade may be adjusted by your professor.\n"
        "- If anything seems off, please contact your professor."
    )
    return _final("partial" if detected_work else "failed", _clamp(base), feedback, report, "\n".join(logs), time.time())


def _choose_model_for_context(context: Dict[str, Any]) -> str:
    """
    Default to a single model for simplicity; can be overridden by env.
    """
    return os.getenv("OPENAI_MODEL", "gpt-5-mini")


def _extract_json(text: str) -> Dict[str, Any]:
    # try to find {...}
    m = re.search(r"\{.*\}", text, flags=re.S)
    if not m:
        return {}
    try:
        return json.loads(m.group(0))
    except Exception:
        return {}


def _clamp(x: float, a: float = 0.0, b: float = 100.0) -> float:
    return max(a, min(b, x))

def _mktempdir(prefix: str = "autograde_") -> Path:
    """Create a temp directory, preferring the shared directory if writable."""
    shared = os.getenv("AUTOGRADER_SHARED_DIR", "/grader_shared")
    try:
        base = Path(shared)
        base.mkdir(parents=True, exist_ok=True)
        if os.access(base, os.W_OK):
            return Path(tempfile.mkdtemp(prefix=prefix, dir=str(base)))
    except Exception:
        pass
    return Path(tempfile.mkdtemp(prefix=prefix))

# -----------------------
# Finalize
# -----------------------
def _final(status: str, grade: float, feedback: str, report: Dict[str, Any], logs: str, start: float) -> Dict[str, Any]:
    return {
        "status": status,
        "grade_pct": _clamp(grade),
        "feedback": feedback.strip(),
        "report": report,
        "logs": logs,
        "finished_at": timezone.now().isoformat(),
        "elapsed_s": max(0.0, time.time() - start),
    }
