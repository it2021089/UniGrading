# assignments/autograder.py
from __future__ import annotations

import json, os, re, time, shutil, tarfile, zipfile, tempfile, mimetypes, subprocess, importlib
from pathlib import Path
from typing import Dict, Any, List, Optional, Tuple

from django.utils import timezone

# ---------------------------
# Optional heavy deps (guard)
# ---------------------------
def _try_import(name: str):
    try:
        return __import__(name)
    except Exception:
        return None

docker = _try_import("docker")
nbformat = _try_import("nbformat")

# pdf/docx/image OCR
_pdfminer_high = None
try:
    _pdfminer_high = __import__("pdfminer.high_level", fromlist=["extract_text"])
except Exception:
    _pdfminer_high = None

docx = _try_import("docx")        # python-docx
PIL = _try_import("PIL")
pytesseract = _try_import("pytesseract")

# ---------------------------
# LLM client (OpenAI style)
# ---------------------------
USE_LLM = os.getenv("AUTOGRADER_USE_LLM", "0") == "1" and bool(os.getenv("OPENAI_API_KEY"))
_openai_client = None
if USE_LLM:
    try:
        openai_mod = importlib.import_module("openai")
        OpenAI = getattr(openai_mod, "OpenAI", None)
        if OpenAI:
            _openai_client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
    except Exception:
        _openai_client = None


# =====================================================================================
# Public API
# =====================================================================================
def grade_submission(assignment, submission) -> Dict[str, Any]:
    """
    Assignment-agnostic grading flow:
      1) Save submission into a shared dir (for Docker runner).
      2) Collect an 'evidence' packet (artifact metadata, text snapshot, runtime results).
      3) Grade with LLM (strict JSON). If unavailable, fall back to generic heuristics.
      4) Return dict: {status, grade_pct, feedback, report, logs, finished_at, elapsed_s}
    """
    t0 = time.time()
    logs_full: str = ""
    try:
        # ---- 1) Place submission into shared dir so dind can mount it
        shared_root = Path(os.getenv("GRADER_SHARED_DIR", "/grader-shared"))
        shared_root.mkdir(parents=True, exist_ok=True)
        tmp_dir = Path(tempfile.mkdtemp(prefix="autograde_", dir=str(shared_root)))
        src_name = Path(getattr(submission.file, "name", "submission.bin")).name
        local_path = tmp_dir / src_name
        with submission.file.open("rb") as f_in, open(local_path, "wb") as f_out:
            shutil.copyfileobj(f_in, f_out)

        # ---- 2) Collect evidence
        evidence, logs_full = _collect_evidence(assignment, submission, tmp_dir, local_path)

        # ---- 3) Grade
        if _openai_client and USE_LLM:
            result = _grade_with_llm(evidence, logs_full)
        else:
            result = _fallback_grade(evidence, logs_full)

        # Finalize envelope
        result.setdefault("report", {})
        result["report"].setdefault("evidence_sizes", _size_info(evidence))
        result["finished_at"] = timezone.now().isoformat()
        result["elapsed_s"] = max(0.0, time.time() - t0)
        return result

    except Exception as e:
        return _final(
            status="failed",
            grade=0.0,
            feedback=f"Autograder crashed unexpectedly: {e}",
            report={"llm_used": False, "crash": str(e)},
            logs=logs_full,
            started=t0,
        )
    finally:
        try:
            shutil.rmtree(tmp_dir, ignore_errors=True)  # type: ignore[name-defined]
        except Exception:
            pass


def apply_result_to_submission(submission, result: Dict[str, Any]) -> None:
    """Copy result fields onto the submission model (if fields exist)."""
    if hasattr(submission, "grade_pct"):
        g = result.get("grade_pct", None)
        submission.grade_pct = float(g) if g is not None else None
    if hasattr(submission, "ai_feedback"):
        submission.ai_feedback = str(result.get("feedback", "") or "")
    if hasattr(submission, "autograde_status"):
        submission.autograde_status = result.get("status", "done")
    if hasattr(submission, "autograde_report"):
        submission.autograde_report = result.get("report", {}) or {}
    if hasattr(submission, "runner_logs"):
        submission.runner_logs = result.get("logs", "") or ""


# =====================================================================================
# Evidence collection
# =====================================================================================
def _collect_evidence(assignment, submission, tmp_dir: Path, local_path: Path) -> Tuple[Dict[str, Any], str]:
    """
    Build a single, assignment-agnostic packet:
      evidence = {
        "assignment": {...},
        "artifact_meta": {...},
        "snapshot": {...},
        "runtime": {...},      # if we tried to run anything
      }
    Returns (evidence, logs_full)
    """
    name = local_path.name
    lower = name.lower()
    mt = mimetypes.guess_type(lower)[0] or "application/octet-stream"
    size_bytes = local_path.stat().st_size if local_path.exists() else 0

    # Assignment info (trim to keep tokens reasonable)
    a_desc = (getattr(assignment, "description", "") or "")[:8000]
    attach_text = _read_assignment_attachment(getattr(assignment, "file", None))

    evidence: Dict[str, Any] = {
        "assignment": {
            "title": getattr(assignment, "title", "")[:200],
            "description": a_desc,
            "attachment_text": attach_text[:8000],
        },
        "artifact_meta": {
            "filename": name,
            "mimetype": mt,
            "size_bytes": size_bytes,
        },
        "snapshot": {},
        "runtime": {},
    }

    # Branch by file type to *collect evidence* (not to decide grades).
    logs_full = ""
    if _is_archive(lower):
        workdir = tmp_dir / "work"
        workdir.mkdir(exist_ok=True)
        extracted = _extract_archive(local_path, workdir)
        file_tree = _list_files(workdir, limit=5000)
        langs = _detect_languages(workdir)
        text_snapshot = _gather_text_snapshot(workdir, limit_bytes=200_000)

        evidence["artifact_meta"].update({"extracted": extracted, "languages": langs, "file_tree": file_tree})
        evidence["snapshot"] = {"text_snapshot": text_snapshot[:200_000]}

        # Try to build/run project (best-effort)
        primary = langs[0]["language"] if langs else _guess_lang_by_tree(file_tree)
        ran, ok, run_logs, exit_code, cmd, image, elapsed = _run_project(workdir, primary)
        logs_full = run_logs
        evidence["runtime"] = {
            "attempted": ran,
            "ok": ok,
            "exit_code": exit_code,
            "elapsed_s": elapsed,
            "image": image,
            "cmd": cmd,
            "logs_tail": _tail(run_logs),
        }

    elif lower.endswith(".ipynb"):
        # Try to execute notebook locally using nbconvert (if present)
        nb_text, ran, ok, run_logs, exit_code, elapsed = _execute_notebook(local_path)
        logs_full = run_logs
        evidence["snapshot"] = {"notebook_text": nb_text[:200_000]}
        evidence["runtime"] = {
            "attempted": ran,
            "ok": ok,
            "exit_code": exit_code,
            "elapsed_s": elapsed,
            "image": None,
            "cmd": ["jupyter", "nbconvert", "--execute"],
            "logs_tail": _tail(run_logs),
        }

    elif _looks_like_code(lower):
        # Run single file in a sandbox (Docker preferred)
        lang = _ext_to_lang(Path(lower).suffix)
        ran, ok, run_logs, exit_code, cmd, image, elapsed = _run_single(local_path, lang)
        logs_full = run_logs
        # Include small code snapshot for LLM context
        code_text = _safe_read_text(local_path, limit=50_000)
        evidence["snapshot"] = {"code_text": code_text[:50_000]}
        evidence["runtime"] = {
            "attempted": ran,
            "ok": ok,
            "exit_code": exit_code,
            "elapsed_s": elapsed,
            "image": image,
            "cmd": cmd,
            "logs_tail": _tail(run_logs),
        }

    elif lower.endswith(".pdf") or "pdf" in mt:
        text = _extract_text_from_pdf(local_path)
        evidence["snapshot"] = {"text": text[:200_000]}
        logs_full = ""

    elif lower.endswith(".docx") or "word" in mt:
        text = _extract_text_from_docx(local_path)
        evidence["snapshot"] = {"text": text[:200_000]}
        logs_full = ""

    elif lower.endswith((".txt", ".md")) or mt.startswith("text/"):
        text = _safe_read_text(local_path, limit=200_000)
        evidence["snapshot"] = {"text": text[:200_000]}
        logs_full = ""

    elif _looks_like_image(lower, mt):
        text = _extract_text_from_image(local_path)
        evidence["snapshot"] = {"image_text": text[:200_000]}
        logs_full = ""

    else:
        # Unknown binary: include tiny peek
        peek = _binary_peek(local_path)
        evidence["snapshot"] = {"binary_peek": peek}
        logs_full = ""

    # Signals for fallback
    evidence["signals"] = {
        "has_text": bool(_measure_textiness(evidence)),
        "ran": bool(evidence.get("runtime", {}).get("attempted")),
        "ok": bool(evidence.get("runtime", {}).get("ok")),
        "exit_code": evidence.get("runtime", {}).get("exit_code"),
        "size_bytes": size_bytes,
    }
    return evidence, logs_full


# =====================================================================================
# Grading strategies
# =====================================================================================
LENIENT_SYSTEM = "You grade from evidence only. Be fair, concise, and return STRICT JSON (no prose)."

def _grade_with_llm(evidence: Dict[str, Any], logs_full: str) -> Dict[str, Any]:
    """
    Single JSON-only call. Let the model infer a light rubric from the assignment description,
    weigh code/text artifacts + runtime signals, then output {grade_pct, status, summary, suggestions[], signals_used{}}.
    """
    # Cap the evidence size sent to the model
    packet = json.dumps(evidence, ensure_ascii=False)
    max_chars = int(os.getenv("AUTOGRADER_LLM_PACKET_CHARS", "12000"))
    packet = packet[:max_chars]

    prompt = f"""
You are a fair grader. Derive a brief rubric from the assignment description and grade LENIENTLY but honestly.

Return STRICT JSON ONLY:
{{
  "grade_pct": float,
  "status": "done" | "partial" | "failed" | "await_manual",
  "summary": str,
  "suggestions": [str],
  "signals_used": {{"ran": bool, "exit_code": int|null, "has_text": bool}}
}}

EVIDENCE (truncated JSON):
{packet}
"""

    try:
        resp = _openai_client.chat.completions.create(
            model=os.getenv("OPENAI_MODEL", "gpt-5-mini"),
            messages=[{"role": "system", "content": LENIENT_SYSTEM},
                      {"role": "user", "content": prompt}],
        )
        txt = resp.choices[0].message.content or "{}"
        data = _extract_json(txt)
        grade = float(data.get("grade_pct", 70.0))
        status = str(data.get("status", "partial"))
        feedback = (data.get("summary", "") or "").strip()
        sugg = data.get("suggestions", []) or []
        if sugg:
            feedback = (feedback + ("\n\nSuggestions:\n- " + "\n- ".join(map(str, sugg)))).strip()

        # Hard guard: if nothing runnable or readable, force fail unless overridden
        if not evidence["signals"]["has_text"] and not evidence["signals"]["ran"]:
            if os.getenv("AUTOGRADER_STRICT_NOFILE_FAIL", "1") == "1":
                grade, status = 0.0, "failed"

        # Cap/clean
        out = _final(
            status=status,
            grade=_clamp(grade),
            feedback=feedback,
            report={
                "llm_used": True,
                "signals_used": data.get("signals_used", {}),
            },
            logs=_trim_logs(logs_full),
            started=time.time() - evidence.get("runtime", {}).get("elapsed_s", 0.0) if evidence.get("runtime") else time.time(),
        )
        return out
    except Exception as e:
        # Fall back if LLM fails
        return _fallback_grade(evidence, logs_full, llm_error=str(e))


def _fallback_grade(evidence: Dict[str, Any], logs_full: str, llm_error: Optional[str] = None) -> Dict[str, Any]:
    """
    Generic, assignment-agnostic heuristics (no hardcoded outputs).
    """
    sig = evidence.get("signals", {})
    ran = bool(sig.get("ran"))
    ok = bool(sig.get("ok"))
    has_text = bool(sig.get("has_text"))

    if not has_text and not ran:
        # Missing/empty artifact: fail hard (prevents the “60% for missing file” issue)
        grade, status = 0.0, "failed"
        feedback = "No readable content or runnable artifact was detected."
    elif ran and ok:
        grade, status = 82.0, "done"
        feedback = "Program ran successfully. Grade estimated from runtime and artifacts."
    elif ran and not ok:
        grade, status = 28.0, "failed"
        feedback = "Program attempted to run but exited with errors."
    else:
        grade, status = 55.0, "partial"
        feedback = "Static review only (no execution)."

    report = {"llm_used": False}
    if llm_error:
        report["llm_error"] = llm_error

    return _final(
        status=status,
        grade=_clamp(grade),
        feedback=feedback,
        report=report,
        logs=_trim_logs(logs_full),
        started=time.time(),
    )


# =====================================================================================
# Sandbox runners
# =====================================================================================
def _docker_available() -> bool:
    return docker is not None and os.getenv("AUTOGRADER_DISABLE_DOCKER", "0") != "1"

def _run_single(local_path: Path, lang: Optional[str]) -> Tuple[bool, bool, str, Optional[int], List[str], Optional[str], float]:
    """
    Run a single source file. Docker first, optional local fallback.
    Returns: (attempted, ok, logs, exit_code, cmd, image, elapsed_s)
    """
    # Docker
    if _docker_available():
        return _docker_run_single(local_path, lang)
    # Local (unsafe; opt-in)
    if os.getenv("AUTOGRADER_ENABLE_LOCAL_EXEC", "0") == "1":
        return _local_run_single(local_path, lang)
    return False, False, "[safe] Execution disabled (no docker, local exec off).", None, [], None, 0.0

def _run_project(workdir: Path, lang: Optional[str]) -> Tuple[bool, bool, str, Optional[int], List[str], Optional[str], float]:
    if _docker_available():
        return _docker_run_project(workdir, lang)
    if os.getenv("AUTOGRADER_ENABLE_LOCAL_EXEC", "0") == "1":
        return _local_run_project(workdir, lang)
    return False, False, "[safe] Execution disabled (no docker, local exec off).", None, [], None, 0.0

# -------------- Docker --------------
def _docker_run_single(local_path: Path, lang: Optional[str]):
    client = docker.from_env()
    img = _image_for_lang(lang, project=False)
    cmd = _cmd_for_single(f"/work/{local_path.name}", lang)
    vols = {str(local_path.parent): {"bind": "/work", "mode": "ro"}}

    t0 = time.time()
    c = client.containers.run(
        img, cmd, detach=True, working_dir="/work",
        network_mode="none", mem_limit="512m", nano_cpus=1_000_000_000, volumes=vols
    )
    try:
        ok = _poll_done(c, timeout=int(os.getenv("AUTOGRADER_TIMEOUT_SEC", "180")))
        code = int(c.attrs.get("State", {}).get("ExitCode", 1))
        logs = c.logs(stdout=True, stderr=True, tail=10000).decode("utf-8", "ignore")
    finally:
        try: c.remove(force=True)
        except Exception: pass
    return True, ok, logs, code, cmd, img, max(0.0, time.time() - t0)

def _docker_run_project(workdir: Path, lang: Optional[str]):
    client = docker.from_env()
    img = _image_for_lang(lang or "python", project=True)
    cmd = _cmd_for_project(lang or "python")
    vols = {str(workdir): {"bind": "/work", "mode": "rw"}}

    t0 = time.time()
    c = client.containers.run(
        img, cmd, detach=True, working_dir="/work",
        network_mode="none", mem_limit="1g", nano_cpus=2_000_000_000, volumes=vols
    )
    try:
        ok = _poll_done(c, timeout=int(os.getenv("AUTOGRADER_TIMEOUT_SEC", "180")))
        code = int(c.attrs.get("State", {}).get("ExitCode", 1))
        logs = c.logs(stdout=True, stderr=True, tail=20000).decode("utf-8", "ignore")
    finally:
        try: c.remove(force=True)
        except Exception: pass
    return True, ok, logs, code, cmd, img, max(0.0, time.time() - t0)

def _poll_done(container, timeout: int) -> bool:
    t0 = time.time()
    while True:
        container.reload()
        st = container.attrs.get("State", {})
        if st.get("Status") in ("exited", "dead"):
            return int(st.get("ExitCode", 1)) == 0
        if time.time() - t0 > timeout:
            try: container.kill()
            except Exception: pass
            return False
        time.sleep(1.0)

# -------------- Local (unsafe; optional) --------------
def _local_run_single(local_path: Path, lang: Optional[str]):
    cmd = _cmd_for_single(str(local_path), lang)
    t0 = time.time()
    cp = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=int(os.getenv("AUTOGRADER_TIMEOUT_SEC", "180")))
    return True, (cp.returncode == 0), cp.stdout, cp.returncode, cmd, None, max(0.0, time.time() - t0)

def _local_run_project(workdir: Path, lang: Optional[str]):
    cmd = _cmd_for_project(lang or "python")
    t0 = time.time()
    cp = subprocess.run(cmd, cwd=workdir, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=int(os.getenv("AUTOGRADER_TIMEOUT_SEC", "180")))
    return True, (cp.returncode == 0), cp.stdout, cp.returncode, cmd, None, max(0.0, time.time() - t0)

# -------------- Commands & images --------------
def _image_for_lang(lang: Optional[str], project: bool) -> str:
    if (lang or "").startswith("python"):
        return os.getenv("AUTOGRADER_DOCKER_IMAGE_DS" if project else "AUTOGRADER_DOCKER_IMAGE_PY", "python:3.11")
    if lang == "node":   return "node:20"
    if lang == "java":   return "maven:3.9-eclipse-temurin-17"
    if lang in ("c","cpp"): return "gcc:13"
    if lang == "go":     return "golang:1.22"
    if lang == "rust":   return "rust:1.79"
    if lang == "ruby":   return "ruby:3.3"
    if lang == "php":    return "php:8.3-cli"
    if lang == "dotnet": return "mcr.microsoft.com/dotnet/sdk:8.0"
    return "python:3.11"

def _cmd_for_single(path_in_container: str, lang: Optional[str]) -> List[str]:
    if (lang or "") == "python" or path_in_container.endswith(".py"):
        return ["python", path_in_container]
    if lang == "bash" or path_in_container.endswith(".sh"):
        return ["bash", path_in_container]
    if lang == "node" or path_in_container.endswith((".js", ".ts")):
        return ["node", path_in_container]
    return ["sh", "-lc", f"echo 'No runner for {path_in_container}'; false"]

def _cmd_for_project(lang: str) -> List[str]:
    if lang == "python":
        return ["sh","-lc","python -V && if [ -f requirements.txt ]; then pip install -q -r requirements.txt; fi && (pytest -q || true) && (python main.py || true)"]
    if lang == "node":
        return ["sh","-lc","node -v && if [ -f package.json ]; then npm ci --silent || npm i --silent; fi && (npm test --silent || npm run start --silent || node . || true)"]
    if lang == "java":
        return ["sh","-lc","if [ -f pom.xml ]; then mvn -q -DskipTests package && java -jar target/*.jar || true; else find -name '*.java' -print -exec javac {{}} + && (java Main || true); fi"]
    if lang in ("c","cpp"):
        return ["sh","-lc","if [ -f Makefile ]; then make -s && ./a.out || true; else (find -name '*.c' -o -name '*.cpp' | xargs -r gcc -O2 -o app) && ./app || true; fi"]
    if lang == "go":
        return ["sh","-lc","go version && (go test ./... || go run . || true)"]
    if lang == "rust":
        return ["sh","-lc","(cargo test -q || cargo run -q || true)"]
    if lang == "dotnet":
        return ["sh","-lc","dotnet --info && (dotnet build -clp:ErrorsOnly || true) && (dotnet test -l:trx || true)"]
    if lang == "ruby":
        return ["sh","-lc","ruby -v && (ruby main.rb || true)"]
    if lang == "php":
        return ["sh","-lc","php -v && (php index.php || true)"]
    return ["sh","-lc","echo 'No project runner for this language'; true"]


# =====================================================================================
# Notebook execution (optional)
# =====================================================================================
def _execute_notebook(nb_path: Path) -> Tuple[str, bool, bool, str, Optional[int], float]:
    """
    Execute a notebook with nbconvert if available.
    Returns: (text_output, attempted, ok, logs, exit_code, elapsed_s)
    """
    if not nbformat:
        txt = _safe_read_text(nb_path, limit=200_000)
        return txt, False, False, "[info] nbformat not installed; static read only.", None, 0.0

    run_dir = nb_path.parent
    executed = run_dir / "executed.ipynb"
    cmd = [
        "python", "-m", "jupyter", "nbconvert",
        "--to", "notebook", "--execute", str(nb_path),
        "--ExecutePreprocessor.timeout=180", "--output", "executed.ipynb"
    ]
    t0 = time.time()
    try:
        cp = subprocess.run(cmd, cwd=run_dir, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=240)
        ok = cp.returncode == 0
        logs = cp.stdout
        txt = ""
        if executed.exists():
            try:
                nb = nbformat.read(executed, as_version=4)
                txt = _notebook_text(nb)
            except Exception:
                txt = "(Could not reopen executed notebook.)"
        return txt, True, ok, logs, cp.returncode, max(0.0, time.time() - t0)
    except Exception as e:
        raw = _safe_read_text(nb_path, limit=200_000)
        return raw, True, False, f"[error] Notebook execution failed: {e}", 1, max(0.0, time.time() - t0)


def _notebook_text(nb) -> str:
    out_lines = []
    for cell in nb.cells:
        if getattr(cell, "cell_type", "") == "code":
            for out in cell.get("outputs", []):
                if "text" in out:
                    out_lines.append(out["text"])
                if "data" in out and "text/plain" in out["data"]:
                    out_lines.append(out["data"]["text/plain"])
    return "\n".join(out_lines)[:200_000]


# =====================================================================================
# File utilities, text extraction, language detect
# =====================================================================================
def _is_archive(lower_name: str) -> bool:
    return lower_name.endswith((".zip", ".tar.gz", ".tgz", ".tar"))

def _looks_like_code(lower_name: str) -> bool:
    return lower_name.endswith((".py",".sh",".js",".ts",".java",".c",".cc",".cpp",".go",".rs",".rb",".php",".cs"))

def _looks_like_image(lower_name: str, mt: str) -> bool:
    return lower_name.endswith((".png",".jpg",".jpeg",".gif",".webp",".bmp",".tiff",".svg")) or mt.startswith("image/")

def _ext_to_lang(ext: str) -> Optional[str]:
    mapping = {
        ".py":"python",".ipynb":"python",".sh":"bash",".js":"node",".ts":"node",".java":"java",
        ".c":"c",".cc":"cpp",".cpp":"cpp",".go":"go",".rs":"rust",".rb":"ruby",".php":"php",".cs":"dotnet"
    }
    return mapping.get(ext)

def _extract_archive(src: Path, dest: Path) -> bool:
    try:
        if src.name.lower().endswith(".zip"):
            with zipfile.ZipFile(src, "r") as zf:
                zf.extractall(dest)
        else:
            with tarfile.open(src, "r:*") as tf:
                tf.extractall(dest)
        return True
    except Exception:
        return False

def _iter_files(root: Path):
    for p in root.rglob("*"):
        if p.is_file():
            yield p

def _list_files(root: Path, limit: int = 5000) -> List[str]:
    out: List[str] = []
    for p in _iter_files(root):
        try:
            out.append(str(p.relative_to(root)))
        except Exception:
            out.append(str(p))
        if len(out) >= limit:
            break
    out.sort()
    return out

def _detect_languages(root: Path) -> List[Dict[str, Any]]:
    scores = {}
    for p in _iter_files(root):
        ext = p.suffix.lower()
        lang = _ext_to_lang(ext)
        if lang:
            scores[lang] = scores.get(lang, 0) + 1
        name = p.name.lower()
        if name in ("pom.xml","build.gradle","package.json","requirements.txt","pyproject.toml","makefile","cmakelists.txt"):
            scores[name] = scores.get(name, 0) + 5
    return [{"language": k, "score": v} for k, v in sorted(scores.items(), key=lambda kv: kv[1], reverse=True)]

def _guess_lang_by_tree(file_tree: List[str]) -> Optional[str]:
    for f in file_tree:
        ext = Path(f).suffix.lower()
        lang = _ext_to_lang(ext)
        if lang:
            return lang
    return None

def _gather_text_snapshot(root: Path, limit_bytes: int = 200_000) -> str:
    chunks, total = [], 0
    for p in _iter_files(root):
        if p.suffix.lower() in (".png",".jpg",".jpeg",".gif",".bmp",".webp",".tiff",".pdf"):
            continue
        try:
            if p.stat().st_size > 50_000:
                continue
            txt = _safe_read_text(p, limit=50_000)
            if txt:
                chunks.append(f"\n--- {p.relative_to(root)} ---\n{txt}\n")
                total += len(txt)
                if total >= limit_bytes:
                    break
        except Exception:
            pass
    return "".join(chunks)

def _read_assignment_attachment(django_file) -> str:
    if not django_file:
        return ""
    try:
        name = Path(django_file.name).name.lower()
        tmpd = Path(tempfile.mkdtemp(prefix="spec_"))
        tmp = tmpd / name
        with django_file.open("rb") as fin, open(tmp, "wb") as fout:
            shutil.copyfileobj(fin, fout)
        try:
            if name.endswith(".pdf") or "pdf" in (mimetypes.guess_type(name)[0] or ""):
                return _extract_text_from_pdf(tmp)
            if name.endswith(".docx") or "word" in (mimetypes.guess_type(name)[0] or ""):
                return _extract_text_from_docx(tmp)
            return _safe_read_text(tmp, limit=200_000)
        finally:
            shutil.rmtree(tmpd, ignore_errors=True)
    except Exception:
        return ""

def _extract_text_from_pdf(path: Path) -> str:
    if not _pdfminer_high:
        return ""
    try:
        return _pdfminer_high.extract_text(str(path)) or ""
    except Exception:
        return ""

def _extract_text_from_docx(path: Path) -> str:
    if not docx:
        return ""
    try:
        d = docx.Document(str(path))
        return "\n".join(par.text for par in d.paragraphs)
    except Exception:
        return ""

def _extract_text_from_image(path: Path) -> str:
    if not PIL:
        return ""
    try:
        from PIL import Image
        img = Image.open(str(path))
        meta = f"(Image {img.size} {img.mode})"
        if pytesseract:
            try:
                return meta + "\n\n" + pytesseract.image_to_string(img)
            except Exception:
                return meta
        return meta
    except Exception:
        return ""

def _binary_peek(path: Path, n: int = 64) -> str:
    try:
        with open(path, "rb") as f:
            return f"(Binary) {f.read(n).hex()}"
    except Exception:
        return ""

def _safe_read_text(path: Path, limit: int = 200_000) -> str:
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            return f.read(limit)
    except Exception:
        return ""


# =====================================================================================
# Small helpers
# =====================================================================================
def _tail(s: str, n: int = 8000) -> str:
    if not s:
        return ""
    return s[-n:]

def _trim_logs(s: str) -> str:
    lim = int(os.getenv("AUTOGRADER_MAX_LOG_BYTES", "200000"))
    if not s:
        return ""
    if len(s) <= lim:
        return s
    # include head + tail to keep context
    head = s[: int(lim * 0.4)]
    tail = s[-int(lim * 0.6):]
    return head + "\n...\n" + tail

def _extract_json(text: str) -> Dict[str, Any]:
    m = re.search(r"\{.*\}", text, flags=re.S)
    if not m:
        return {}
    try:
        return json.loads(m.group(0))
    except Exception:
        return {}

def _size_info(evidence: Dict[str, Any]) -> Dict[str, int]:
    s = json.dumps(evidence, ensure_ascii=False)
    return {"chars": len(s)}

def _measure_textiness(evidence: Dict[str, Any]) -> int:
    snap = evidence.get("snapshot", {})
    text_fields = ["text", "text_snapshot", "code_text", "image_text", "notebook_text", "binary_peek"]
    total = 0
    for k in text_fields:
        v = snap.get(k)
        if isinstance(v, str):
            total += len(v)
    return total

def _clamp(x: float, a: float = 0.0, b: float = 100.0) -> float:
    return max(a, min(b, x))

def _final(status: str, grade: float, feedback: str, report: Dict[str, Any], logs: str, started: float) -> Dict[str, Any]:
    return {
        "status": status,
        "grade_pct": _clamp(grade),
        "feedback": (feedback or "").strip(),
        "report": report or {},
        "logs": logs or "",
        "finished_at": timezone.now().isoformat(),
        "elapsed_s": max(0.0, time.time() - (started if isinstance(started, (int, float)) else time.time())),
    }
