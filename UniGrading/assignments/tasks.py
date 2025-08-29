# assignments/tasks.py
from __future__ import annotations

import os
import logging
from celery import shared_task
from django.db import transaction
from django.core.exceptions import ObjectDoesNotExist

from .models import AssignmentSubmission
from .autograder import grade_submission, apply_result_to_submission

logger = logging.getLogger(__name__)


def _llm_available() -> bool:
    use_llm = os.getenv("AUTOGRADER_USE_LLM", "0") == "1"
    api_key = os.getenv("OPENAI_API_KEY", "")
    return bool(use_llm and api_key)


@shared_task(bind=True, autoretry_for=(Exception,), retry_backoff=True, max_retries=3)
def run_autograde(self, submission_id: int) -> dict:
    try:
        sub = AssignmentSubmission.objects.select_related("assignment").get(pk=submission_id)
    except ObjectDoesNotExist:
        logger.warning("Submission %s not found", submission_id)
        return {"ok": False, "error": "submission_not_found"}

    a = sub.assignment

    # mark running if the field exists
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

    return {"ok": True, "status": sub.autograde_status, "grade": getattr(sub, "grade_pct", None)}
