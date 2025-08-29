# assignments/autograder.py
"""
Universal, lenient autograder that accepts ANY file type:

- Archives (.zip, .tar.gz, .tgz): unpack, detect languages, try to build/run,
  else text review.
- Notebooks (.ipynb / Google Colab): execute offline with nbconvert, then review.
- Single code files (.py, .sh, .js, .java, .c/.cpp, etc.): run inside Docker
  (if available) or optional local runner; capture output and review.
- PDFs / DOCX / TXT / MD: extract text and review.
- Images: optional OCR (pytesseract) or metadata-based review.

It fails softly. **If the LLM is unavailable or errors, NO grade is assigned** and
the submission is marked for manual review.

Env flags (all optional):
- AUTOGRADER_DISABLE_DOCKER=1       -> skip docker; try local (unsafe)
- AUTOGRADER_ENABLE_LOCAL_EXEC=1    -> allow local subprocess fallback
- AUTOGRADER_USE_LLM=1              -> enable OpenAI-based feedback/grade if OPENAI_API_KEY is set
- AUTOGRADER_DOCKER_IMAGE_PY        -> override default Python image
- AUTOGRADER_DOCKER_IMAGE_DS        -> override data-science image (notebook runner)
"""

from __future__ import annotations

import os
import re
import json
import time
import shutil
import tarfile
import zipfile
import tempfile
import mimetypes
import subprocess
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
_openai = _try_import("openai")
USE_LLM = bool(os.getenv("AUTOGRADER_USE_LLM", "0") == "1") and bool(os.getenv("OPENAI_API_KEY"))
if _openai and USE_LLM:
    try:
        from openai import OpenAI  # modern SDK
        _openai_client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
    except Exception:
        _openai_client = None
else:
    _openai_client = None


# -----------------------
# Public API
# -----------------------
def grade_submission(assignment, submission) -> Dict[str, Any]:
    """
    Entry point. Downloads the submission file to a temp dir, decides the pathway,
    runs best-effort checks, and returns a dict:
      {
        "status": "done" | "partial" | "failed" | "manual",
        "grade_pct": float | None,
        "feedback": str,
        "report": {...},
        "logs": str
      }
    If the LLM is unavailable or errors, **grade_pct is None** and status is "manual".
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
    tmp_dir = Path(tempfile.mkdtemp(prefix="autograde_"))
    local_path = tmp_dir / "submission.bin"
    try:
        f = submission.file.open("rb")  # storage-agnostic file object
        with open(local_path, "wb") as out:
            shutil.copyfileobj(f, out)
        f.close()
    except Exception as e:
        logs.append(f"[error] Could not read submission from storage: {e}")
        return _final("failed", None, "Could not read your file from storage.", report, "\n".join(logs), start)

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
            text = _best_effort_binary_peek(local_path, logs)
            res = _llm_grade_textual(text, spec_text, spec_attachment_text, context={"type": "binary"}, logs=logs, report=report)

    except Exception as e:
        logs.append(f"[error] Pipeline crashed: {e}")
        res = _final(
            "manual",
            None,
            "We couldn’t fully analyze your file automatically. It has been queued for manual review.",
            report,
            "\n".join(logs),
            start,
        )

    # Clean temp
    try:
        shutil.rmtree(tmp_dir, ignore_errors=True)
    except Exception:
        pass

    return res


def apply_result_to_submission(submission, result: Dict[str, Any]) -> None:
    """Persist the result to the submission model, if fields exist."""
    grade_val = result.get("grade_pct", None)
    if hasattr(submission, "grade_pct"):
        submission.grade_pct = float(grade_val) if isinstance(grade_val, (int, float)) else None
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
    if hasattr(submission, "graded_at"):
        try:
            # Only set graded_at if we actually have a numeric grade
            submission.graded_at = timezone.now() if isinstance(grade_val, (int, float)) else None
        except Exception:
            pass


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
    report["file_tree"] = files[:3000]
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

    # Append a tiny eval cell (best-effort)
    try:
        nb = nbformat.read(nb_in, as_version=4)
        nb.cells.append(nbformat.v4.new_code_cell("print('OK')  # evaluation hint"))
        nbformat.write(nb, nb_in)
    except Exception as e:
        logs.append(f"[warn] Could not append eval cell: {e}")

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

        executed = (run_dir / "executed.ipynb") if not sourced else (nb_in.parent / "executed.ipynb")
        if executed.exists():
            try:
                nb2 = nbformat.read(executed, as_version=4)
                out_text = _notebook_text(nb2)
            except Exception:
                out_text = "(Could not re-open executed notebook; using logs only.)"
        else:
            out_text = "(No executed notebook produced; using logs only.)"

        text_for_llm = f"NOTEBOOK OUTPUT/LOGS:\n{out_text}\n\nRUN LOG TAIL:\n{logs[-1] if logs else ''}"
        res = _llm_grade_textual(text_for_llm, spec_text, spec_attach, context={"type": "ipynb-exec"}, logs=logs, report=report)
        # We no longer boost grade here; LLM will decide. If LLM unavailable, res will be manual.
        return res
    except Exception as e:
        logs.append(f"[warn] Notebook execution failed: {e}")
        text = _safe_read_text(nb_in, logs)
        return _llm_grade_textual(text, spec_text, spec_attach, context={"type": "ipynb-static"}, logs=logs, report=report)


def _handle_single_code(tmp_dir: Path, local_path: Path, filename: str, spec_text: str, spec_attach: str,
                        logs: List[str], report: Dict[str, Any]) -> Dict[str, Any]:
    report["detected_work"] = True
    lang = _ext_to_lang(Path(filename).suffix.lower())
    ran, ok, run_logs = _run_single_file_in_sandbox(local_path, lang, timeout=60)
    logs.append(run_logs[-4000:])
    context = {"type": f"single-{lang or 'code'}"}
    text_for_llm = f"RUNTIME OUTPUT TAIL:\n{run_logs[-2000:]}"
    # If LLM unavailable, this will return status="manual" with no grade
    return _llm_grade_textual(text_for_llm, spec_text, spec_attach, context=context, logs=logs, report=report)


def _build_and_run_project(workdir: Path, lang: str, spec_text: str, spec_attach: str,
                           logs: List[str], report: Dict[str, Any]) -> Dict[str, Any]:
    ran, ok, run_logs = _run_project_in_sandbox(workdir, lang, timeout=180)
    logs.append(run_logs[-4000:])
    context = {"type": f"project-{lang}"}
    tree = "\n".join(report.get("file_tree", [])[:3000])
    text_for_llm = f"BUILD/RUN LOGS TAIL:\n{run_logs[-2500:]}\n\nFILE TREE (first 3000 lines):\n{tree}"
    return _llm_grade_textual(text_for_llm, spec_text, spec_attach, context=context, logs=logs, report=report)


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
    paths.sort()
    return paths

def _detect_languages(root: Path) -> List[Dict[str, Any]]:
    counts = {}
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

# ---- Docker runners (ContainerError-aware) ----
def _docker_run_single(path: Path, lang: Optional[str], timeout: int):
    client = docker.from_env()
    mounts = {str(path.parent): {"bind": "/work", "mode": "ro"}}
    cmd = _lang_cmd_single("/work/" + path.name, lang)
    image = _choose_image_for_lang(lang)
    try:
        logs = client.containers.run(
            image, cmd, detach=False, remove=True, working_dir="/work",
            network_disabled=True, mem_limit="512m", stderr=True, stdout=True,
            nano_cpus=1_000_000_000, volumes=mounts
        )
        text = logs.decode("utf-8", errors="ignore")
        return True, True, text
    except Exception as e:
        from docker.errors import ContainerError
        if isinstance(e, ContainerError):
            out = (e.stderr or b"").decode("utf-8", errors="ignore")
            return True, False, out
        return False, False, f"[docker] run failed: {e}"

def _docker_run_project(workdir: Path, lang: str, timeout: int):
    client = docker.from_env()
    mounts = {str(workdir): {"bind": "/work", "mode": "rw"}}
    cmd = _lang_cmd_project(lang)
    image = _choose_image_for_lang(lang, project=True)
    try:
        logs = client.containers.run(
            image, cmd, detach=False, remove=True, working_dir="/work",
            network_disabled=True, mem_limit="1g", stderr=True, stdout=True,
            nano_cpus=2_000_000_000, volumes=mounts
        )
        text = logs.decode("utf-8", errors="ignore")
        return True, True, text
    except Exception as e:
        from docker.errors import ContainerError
        if isinstance(e, ContainerError):
            out = (e.stderr or b"").decode("utf-8", errors="ignore")
            return True, False, out
        return False, False, f"[docker] run failed: {e}"

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
        return ["node", file_in_container]
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


# -----------------------
# Text extraction
# -----------------------
def _extract_text_from_arbitrary_file(django_file, logs: List[str]) -> str:
    name = Path(django_file.name).name.lower()
    mt = mimetypes.guess_type(name)[0] or ""
    try:
        tmp_path = Path(tempfile.mkdtemp(prefix="spec_")) / name
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
        return ""
    finally:
        try:
            shutil.rmtree(tmp_path.parent, ignore_errors=True)
        except Exception:
            pass

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
# LLM grading (with manual fallback)
# -----------------------
LENIENT_SYSTEM = """You are a gentle, fair grader for programming assignments.
Be LENIENT. Small mistakes (formatting, naming, minor inefficiencies) should only reduce the grade slightly.
Focus on core correctness and whether the submission plausibly satisfies the assignment.
Return clear, constructive, encouraging feedback.
"""

def _llm_grade_textual(student_text: str, spec_text: str, spec_attach: str, context: Dict[str, Any],
                       logs: List[str], report: Dict[str, Any]) -> Dict[str, Any]:
    """
    If LLM is available, get a numeric grade plus feedback.
    If LLM is unavailable or errors, return status='manual' with NO grade.
    """
    # Note: we still set a bit of context so professors have something to read.
    # But we do NOT compute a numeric grade without LLM.
    if not (_openai_client and USE_LLM):
        logs.append("[info] LLM disabled/unavailable: moving to manual review (no grade assigned).")
        # Capture a small excerpt of student_text to help manual graders
        excerpt = (student_text or "")[:4000]
        report["text_excerpt"] = excerpt
        return _final(
            "manual",
            None,
            "Automatic grading is unavailable (LLM disabled or unreachable). Your submission will be reviewed manually.",
            report,
            "\n".join(logs),
            time.time(),
            already_started=True,
        )

    try:
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
            temperature=0.2,
        )
        text = resp.choices[0].message.content or ""
        data = _extract_json(text)
        grade = data.get("grade_pct", None)
        feedback = f"{data.get('summary','')}\n\nSuggestions:\n- " + "\n- ".join(data.get("suggestions", []))
        # Only treat as "done" if a numeric grade is present
        if isinstance(grade, (int, float)):
            return _final("done", float(grade), feedback, report, "\n".join(logs), time.time(), already_started=True)
        else:
            return _final(
                "manual",
                None,
                "The grader could not assign a numeric score. Manual review required.",
                report,
                "\n".join(logs),
                time.time(),
                already_started=True,
            )
    except Exception as e:
        logs.append(f"[warn] LLM call failed: {e}")
        # Explicit manual fallback on LLM failure
        excerpt = (student_text or "")[:4000]
        report["text_excerpt"] = excerpt
        return _final(
            "manual",
            None,
            "Automatic grading failed due to a grading service issue. Manual review required.",
            report,
            "\n".join(logs),
            time.time(),
            already_started=True,
        )

def _choose_model_for_context(context: Dict[str, Any]) -> str:
    t = context.get("type", "")
    if any(k in t for k in ("single-", "project-", "ipynb-")):
        return os.getenv("OPENAI_CODE_MODEL", "gpt-4o-mini")
    return os.getenv("OPENAI_TEXT_MODEL", "gpt-4o-mini")

def _extract_json(text: str) -> Dict[str, Any]:
    m = re.search(r"\{.*\}", text, flags=re.S)
    if not m:
        return {}
    try:
        return json.loads(m.group(0))
    except Exception:
        return {}

# -----------------------
# Finalize
# -----------------------
def _final(status: str, grade: Optional[float], feedback: str, report: Dict[str, Any], logs: str, start: float, already_started: bool=False) -> Dict[str, Any]:
    return {
        "status": status,
        "grade_pct": (float(grade) if isinstance(grade, (int, float)) else None),
        "feedback": (feedback or "").strip(),
        "report": report,
        "logs": logs,
        "finished_at": timezone.now().isoformat(),
        "elapsed_s": (time.time() - start)
    }
