# assignments/tasks.py
from __future__ import annotations

import os, logging
from celery import shared_task  # type: ignore
from django.db import transaction
from django.core.exceptions import ObjectDoesNotExist
from django.utils import timezone

from .models import AssignmentSubmission, Assignment
from .autograder import grade_submission, apply_result_to_submission

logger = logging.getLogger(__name__)

def _llm_available() -> bool:
    return os.getenv("AUTOGRADER_USE_LLM", "0") == "1" and bool(os.getenv("OPENAI_API_KEY"))

@shared_task(bind=True, autoretry_for=(Exception,), retry_backoff=True, max_retries=3)
def run_autograde(self, submission_id: int) -> dict:
    try:
        sub = AssignmentSubmission.objects.select_related("assignment").get(pk=submission_id)
    except ObjectDoesNotExist:
        logger.warning("Submission %s not found", submission_id)
        return {"ok": False, "error": "submission_not_found"}

    a = sub.assignment

    if hasattr(sub, "autograde_status"):
        sub.autograde_status = "running"
        sub.save(update_fields=["autograde_status"])

    result = grade_submission(assignment=a, submission=sub)

    require_llm_for_grade = os.getenv("AUTOGRADER_REQUIRE_LLM", "0") == "1"  # default OFF
    llm_ok = _llm_available()
    llm_used = bool(result.get("report", {}).get("llm_used", False))
    llm_error = bool(result.get("report", {}).get("llm_error", ""))

    hold_for_manual = require_llm_for_grade and (not llm_ok or not llm_used or llm_error)

    with transaction.atomic():
        if hold_for_manual:
            if hasattr(sub, "ai_feedback"):
                sub.ai_feedback = result.get("feedback", "") or ""
            if hasattr(sub, "runner_logs"):
                sub.runner_logs = result.get("logs", "") or ""
            if hasattr(sub, "autograde_report"):
                sub.autograde_report = result.get("report", {}) or {}
            if hasattr(sub, "grade_pct"):
                sub.grade_pct = None
            if hasattr(sub, "autograde_status"):
                sub.autograde_status = "await_manual"
            sub.save()
            return {"ok": True, "status": "await_manual"}

        apply_result_to_submission(sub, result)
        if hasattr(sub, "autograde_status"):
            sub.autograde_status = result.get("status") or ("done" if (sub.grade_pct or 0) >= 50 else "failed")
        sub.save()

    return {"ok": True, "status": getattr(sub, "autograde_status", "done"), "grade": getattr(sub, "grade_pct", None)}

@shared_task(bind=True, autoretry_for=(Exception,), retry_backoff=True, max_retries=3)
def run_autograde_for_assignment(self, assignment_id: int) -> dict:
    try:
        a = Assignment.objects.get(pk=assignment_id)
    except ObjectDoesNotExist:
        return {"ok": False, "error": "assignment_not_found"}

    # Never re-run if marked done
    if getattr(a, "autograde_done_at", None):
        return {"ok": True, "status": "already_done"}

    subs = AssignmentSubmission.objects.filter(assignment=a).only("id")
    ids = list(subs.values_list("id", flat=True))
    for sid in ids:
        run_autograde.delay(sid)

    # Mark assignment as completed right after dispatch so Beat won't re-enqueue.
    Assignment.objects.filter(pk=a.id, autograde_done_at__isnull=True).update(autograde_done_at=timezone.now())

    return {"ok": True, "assignment": a.id, "dispatched": len(ids), "completed": True}

def ensure_autograde_scheduled(assignment_id: int) -> bool:
    try:
        a = Assignment.objects.get(pk=assignment_id)
    except Assignment.DoesNotExist:
        logger.warning("ensure_autograde_scheduled: assignment %s not found", assignment_id)
        return False

    if not a.autograde_enabled:
        return False
    if getattr(a, "autograde_done_at", None):
        return False
    if a.autograde_job_scheduled:
        return True

    now = timezone.now()
    if a.due_date <= now:
        run_autograde_for_assignment.delay(a.id)
        a.autograde_job_scheduled = True
        a.save(update_fields=["autograde_job_scheduled"])
        logger.info("Dispatched immediate autograde for assignment %s (past due).", a.id)
        return True

    run_autograde_for_assignment.apply_async(args=(a.id,), eta=a.due_date)
    a.autograde_job_scheduled = True
    a.save(update_fields=["autograde_job_scheduled"])
    logger.info("Scheduled autograde for assignment %s at %s.", a.id, a.due_date)
    return True

@shared_task(bind=True)
def enqueue_due_autogrades(self) -> dict:
    """
    Safety net: pick up due assignments that are enabled, not done, and not yet scheduled.
    """
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
