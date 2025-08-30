# assignments/tasks.py
from __future__ import annotations

import logging
from celery import shared_task
from django.db import transaction
from django.core.exceptions import ObjectDoesNotExist

from .models import AssignmentSubmission
from .autograder import grade_submission, apply_result_to_submission

logger = logging.getLogger(__name__)


@shared_task(bind=True, autoretry_for=(Exception,), retry_backoff=True, max_retries=3)
def run_autograde(self, submission_id: int) -> dict:
    """
    Celery task to run the autograder asynchronously.

    Contract with autograder:
    - If LLM is unavailable or errors, autograder returns result with grade_pct=None
      and status 'failed' plus human-friendly feedback. We then keep grade NULL and
      mark the submission as 'failed' (meaning: manual review required).
    - If grade_pct is present, we persist it and mark status 'done' (or 'failed'
      if autograder reported a hard failure).
    """
    try:
        sub = AssignmentSubmission.objects.select_related("assignment").get(pk=submission_id)
    except ObjectDoesNotExist:
        logger.warning("Submission %s not found", submission_id)
        return {"ok": False, "error": "submission_not_found"}

    a = sub.assignment

    # mark running (if field exists)
    if hasattr(sub, "autograde_status"):
        sub.autograde_status = "running"
        sub.save(update_fields=["autograde_status"])

    # run autograder
    result = grade_submission(assignment=a, submission=sub)

    manual_needed = (result.get("grade_pct") is None)

    with transaction.atomic():
        if manual_needed:
            # Keep artifacts, but DO NOT assign a numeric grade.
            if hasattr(sub, "ai_feedback"):
                sub.ai_feedback = result.get("feedback", "") or ""
            if hasattr(sub, "runner_logs"):
                sub.runner_logs = result.get("logs", "") or ""
            if hasattr(sub, "autograde_report"):
                sub.autograde_report = result.get("report", {}) or {}
            if hasattr(sub, "grade_pct"):
                sub.grade_pct = None
            if hasattr(sub, "autograde_status"):
                # Use 'failed' to represent "awaiting manual review" (valid choice).
                sub.autograde_status = "failed"
            sub.save()
            return {
                "ok": True,
                "status": sub.autograde_status,
                "manual_review": True,
                "grade": None,
            }

        # Otherwise apply the grade normally
        apply_result_to_submission(sub, result)
        if hasattr(sub, "autograde_status"):
            sub.autograde_status = "done" if result.get("status") in ("done", "partial") else "failed"
        sub.save()

    return {"ok": True, "status": sub.autograde_status, "grade": getattr(sub, "grade_pct", None)}
