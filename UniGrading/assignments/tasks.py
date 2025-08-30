# assignments/tasks.py
from __future__ import annotations

import os
import logging
from celery import shared_task #type: ignore
from django.db import transaction
from django.core.exceptions import ObjectDoesNotExist
from django.utils import timezone

from .models import AssignmentSubmission, Assignment
from .autograder import grade_submission, apply_result_to_submission

logger = logging.getLogger(__name__)


def _llm_available() -> bool:
    use_llm = os.getenv("AUTOGRADER_USE_LLM", "0") == "1"
    api_key = os.getenv("OPENAI_API_KEY", "")
    return bool(use_llm and api_key)


@shared_task(bind=True, autoretry_for=(Exception,), retry_backoff=True, max_retries=3)
def run_autograde(self, submission_id: int) -> dict:
    """
    Grade a single submission. If LLM is required (AUTOGRADER_REQUIRE_LLM=1) and
    is unavailable/failed, do NOT set a grade and mark await_manual.
    """
    try:
        sub = AssignmentSubmission.objects.select_related("assignment").get(pk=submission_id)
    except ObjectDoesNotExist:
        logger.warning("Submission %s not found", submission_id)
        return {"ok": False, "error": "submission_not_found"}

    a = sub.assignment

    # mark running
    if hasattr(sub, "autograde_status"):
        sub.autograde_status = "running"
        sub.save(update_fields=["autograde_status"])

    result = grade_submission(assignment=a, submission=sub)

    require_llm_for_grade = os.getenv("AUTOGRADER_REQUIRE_LLM", "1") == "1"
    llm_ok = _llm_available()
    llm_used = bool(result.get("report", {}).get("llm_used", False))
    llm_error = bool(result.get("report", {}).get("llm_error", ""))

    hold_for_manual = require_llm_for_grade and (not llm_ok or not llm_used or llm_error)

    with transaction.atomic():
        if hold_for_manual:
            # keep artifacts but DO NOT set grade
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
            return {"ok": True, "status": "await_manual", "reason": "llm_unavailable_or_failed"}

        # otherwise apply the grade
        apply_result_to_submission(sub, result)
        if hasattr(sub, "autograde_status"):
            sub.autograde_status = "done" if result.get("status") in ("done", "partial") else "failed"
        sub.save()

    return {"ok": True, "status": getattr(sub, "autograde_status", "done"), "grade": getattr(sub, "grade_pct", None)}


@shared_task(bind=True, autoretry_for=(Exception,), retry_backoff=True, max_retries=3)
def run_autograde_for_assignment(self, assignment_id: int) -> dict:
    """
    Grade all submissions for an assignment (once).
    If already completed, exit quickly.
    """
    try:
        a = Assignment.objects.get(pk=assignment_id)
    except ObjectDoesNotExist:
        return {"ok": False, "error": "assignment_not_found"}

    # If someone already finished this assignment, don't run again.
    if getattr(a, "autograde_done_at", None):
        return {"ok": True, "status": "already_done"}

    subs = AssignmentSubmission.objects.filter(assignment=a).select_related("student")
    total = subs.count()
    graded = await_manual = failed = 0

    for sub in subs:
        try:
            # Run one-by-one; your existing run_autograde() is already idempotent per submission row
            res = run_autograde.apply_async(args=(sub.id,)).get(disable_sync_subtasks=False)
            status = res.get("status")
            if status == "await_manual":
                await_manual += 1
            elif status in ("done", "partial"):
                graded += 1
            else:
                failed += 1
        except Exception:
            failed += 1

    # Mark the assignment as completed so it will never be scheduled again.
    Assignment.objects.filter(pk=a.id, autograde_done_at__isnull=True).update(autograde_done_at=timezone.now())

    return {
        "ok": True,
        "assignment": a.id,
        "total": total,
        "graded": graded,
        "await_manual": await_manual,
        "failed": failed,
        "completed": True,
    }

def ensure_autograde_scheduled(assignment_id: int) -> bool:
    """
    Schedule exactly one 'grade-at-deadline' job for an assignment.
    Never schedule if the assignment has already been graded.
    """
    try:
        a = Assignment.objects.get(pk=assignment_id)
    except Assignment.DoesNotExist:
        logger.warning("ensure_autograde_scheduled: assignment %s not found", assignment_id)
        return False

    if not a.autograde_enabled:
        return False

    # Already finished? never schedule again.
    if getattr(a, "autograde_done_at", None):
        return False

    # Already scheduled? fine.
    if a.autograde_job_scheduled:
        return True

    now = timezone.now()
    if a.due_date <= now:
        # Past due: dispatch immediately
        run_autograde_for_assignment.delay(a.id)
        a.autograde_job_scheduled = True
        a.save(update_fields=["autograde_job_scheduled"])
        logger.info("Dispatched immediate autograde for assignment %s (past due).", a.id)
        return True

    # Future due: schedule for eta
    run_autograde_for_assignment.apply_async(args=(a.id,), eta=a.due_date)
    a.autograde_job_scheduled = True
    a.save(update_fields=["autograde_job_scheduled"])
    logger.info("Scheduled autograde for assignment %s at %s.", a.id, a.due_date)
    return True


@shared_task(bind=True)
def enqueue_due_autogrades(self) -> dict:
    """
    Safety net: every minute pick up due assignments that were never scheduled.
    Only consider assignments that are not done yet.
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