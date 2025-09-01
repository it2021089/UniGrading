# assignments/tasks.py
from __future__ import annotations

import os, logging
from celery import shared_task
from django.db import transaction
from django.utils import timezone
from django.core.exceptions import ObjectDoesNotExist

from .models import AssignmentSubmission, Assignment
from .autograder import grade_submission, apply_result_to_submission

logger = logging.getLogger(__name__)

def _llm_available() -> bool:
    return os.getenv("AUTOGRADER_USE_LLM", "0") == "1" and bool(os.getenv("OPENAI_API_KEY"))

@shared_task(bind=True, autoretry_for=(Exception,), retry_backoff=True, max_retries=3)
def run_autograde(self, submission_id: int) -> dict:
    """Grade a single submission; on low LLM confidence or failure, keep artifacts and mark await_manual."""
    try:
        sub = AssignmentSubmission.objects.select_related("assignment").get(pk=submission_id)
    except ObjectDoesNotExist:
        logger.warning("run_autograde: submission %s not found", submission_id)
        return {"ok": False, "error": "submission_not_found"}

    a = sub.assignment
    if hasattr(sub, "autograde_status"):
        sub.autograde_status = "running"
        sub.save(update_fields=["autograde_status"])

    result = grade_submission(a, sub)

    require_llm = os.getenv("AUTOGRADER_REQUIRE_LLM", "1") == "1"
    llm_ok = _llm_available()
    r = result.get("report", {}) or {}
    needs_manual = r.get("llm_needs_manual", False)
    llm_used = r.get("llm_used", False)
    llm_error = bool(r.get("llm_error", ""))

    hold = require_llm and (not llm_ok or not llm_used or llm_error or needs_manual)

    with transaction.atomic():
        if hold:
            # keep comments and logs but withhold numeric grade
            if hasattr(sub, "ai_feedback"): sub.ai_feedback = result.get("feedback", "") or ""
            if hasattr(sub, "runner_logs"): sub.runner_logs = result.get("logs", "") or ""
            if hasattr(sub, "autograde_report"): sub.autograde_report = r
            if hasattr(sub, "grade_pct"): sub.grade_pct = None
            if hasattr(sub, "autograde_status"): sub.autograde_status = "await_manual"
            sub.save()
            return {"ok": True, "status": "await_manual"}

        # normal path
        apply_result_to_submission(sub, result)
        if hasattr(sub, "autograde_status"):
            sub.autograde_status = "done" if result.get("status") in ("done", "partial") else "failed"
        sub.save()

    return {"ok": True, "status": getattr(sub, "autograde_status", "done"), "grade": getattr(sub, "grade_pct", None)}

@shared_task(bind=True, autoretry_for=(Exception,), retry_backoff=True, max_retries=3)
def run_autograde_for_assignment(self, assignment_id: int) -> dict:
    """Grade all submissions once; then mark assignment done to avoid future scheduling."""
    try:
        a = Assignment.objects.get(pk=assignment_id)
    except ObjectDoesNotExist:
        return {"ok": False, "error": "assignment_not_found"}

    if getattr(a, "autograde_done_at", None):
        return {"ok": True, "status": "already_done"}

    subs = AssignmentSubmission.objects.filter(assignment=a).select_related("student")
    total = subs.count()
    graded = await_manual = failed = 0

    for sub in subs:
        try:
            res = run_autograde.apply_async(args=(sub.id,)).get(disable_sync_subtasks=False)
            s = res.get("status")
            if s == "await_manual": await_manual += 1
            elif s in ("done", "partial"): graded += 1
            else: failed += 1
        except Exception as e:
            logger.warning("run_autograde_for_assignment: submission %s error: %s", sub.id, e)
            failed += 1

    Assignment.objects.filter(pk=a.id, autograde_done_at__isnull=True).update(autograde_done_at=timezone.now())
    return {"ok": True, "assignment": a.id, "total": total, "graded": graded, "await_manual": await_manual, "failed": failed, "completed": True}

def ensure_autograde_scheduled(assignment_id: int) -> bool:
    """Schedule one run at due_date; if past due, dispatch immediately. Idempotent via flags."""
    try:
        a = Assignment.objects.get(pk=assignment_id)
    except Assignment.DoesNotExist:
        logger.warning("ensure_autograde_scheduled: assignment %s not found", assignment_id)
        return False

    if not a.autograde_enabled: return False
    if getattr(a, "autograde_done_at", None): return False
    if a.autograde_job_scheduled: return True

    now = timezone.now()
    if a.due_date <= now:
        run_autograde_for_assignment.delay(a.id)
        a.autograde_job_scheduled = True
        a.save(update_fields=["autograde_job_scheduled"])
        return True

    run_autograde_for_assignment.apply_async(args=(a.id,), eta=a.due_date)
    a.autograde_job_scheduled = True
    a.save(update_fields=["autograde_job_scheduled"])
    return True

@shared_task(bind=True)
def enqueue_due_autogrades(self) -> dict:
    """Beat safety-net: every minute pick due assignments never scheduled."""
    now = timezone.now()
    qs = Assignment.objects.filter(
        autograde_enabled=True,
        autograde_job_scheduled=False,
        autograde_done_at__isnull=True,
        due_date__lte=now,
    ).only("id")
    dispatched = 0
    for a in qs:
        run_autograde_for_assignment.delay(a.id)
        Assignment.objects.filter(pk=a.id).update(autograde_job_scheduled=True)
        dispatched += 1
    return {"ok": True, "dispatched": dispatched, "ts": now.isoformat()}
