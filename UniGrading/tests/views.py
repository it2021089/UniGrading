# tests/views.py
from __future__ import annotations

import json
from statistics import median

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db.models import Avg, F
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse, reverse_lazy
from django.utils import timezone
from django.views.decorators.http import require_http_methods

from subjects.models import Subject, Enrollment
from .forms import TestForm
from .models import (
    Test,
    Question,
    Choice,
    TestAttempt,
    AttemptAnswer,
)


# -----------------
# Helpers
# -----------------
def _is_owner_prof(user, subject: Subject) -> bool:
    return getattr(user, "role", None) == "professor" and subject.professor_id == user.id


def _is_enrolled(user, subject: Subject) -> bool:
    return Enrollment.objects.filter(user=user, subject=subject).exists()


# -----------------
# List tests (prof & student)
# -----------------
@login_required
def my_tests(request, subject_id: int):
    """
    Professors (subject owners): see/manage their tests.
    Students (and non-owner professors enrolled): see tests they can take or their score if done.
    """
    subject = get_object_or_404(Subject, pk=subject_id)
    can_manage = _is_owner_prof(request.user, subject)

    if can_manage:
        tests = (
            Test.objects.filter(subject=subject, professor=request.user)
            .prefetch_related("questions")
            .order_by("-created_at")
        )
    else:
        # Must be enrolled (student or non-owner prof)
        if not _is_enrolled(request.user, subject):
            messages.error(request, "You must be enrolled to view tests for this subject.")
            return redirect("subjects:subject_detail", pk=subject.id)

        tests = (
            Test.objects.filter(subject=subject)
            .select_related("professor")
            .prefetch_related("questions")
            .order_by("name")
        )

    # Build maps for student/non-owner view: can_take, score %, and attempt id
    user_attempts = {
        a.test_id: a
        for a in TestAttempt.objects.filter(test__in=tests, student=request.user)
    }
    for t in tests:
        att = user_attempts.get(t.id)
        if att:
            t.can_take = False
            t.score_pct = round((att.score * 100.0) / att.max_score, 2) if att.max_score else 0.0
            t.user_attempt_id = att.id
        else:
            t.can_take = True
            t.score_pct = None
            t.user_attempt_id = None

    breadcrumbs = [
        ("Dashboard", reverse_lazy("users:dashboard")),
        ("My Subjects", reverse_lazy("subjects:my_subjects")),
        (f"Subject: {subject.name}", reverse_lazy("subjects:subject_detail", args=[subject.id])),
        ("Tests", ""),
    ]

    return render(
        request,
        "my_tests.html",
        {
            "subject": subject,
            "tests": tests,
            "can_manage": can_manage,
            "breadcrumbs": breadcrumbs,
        },
    )


# -----------------
# Create / Edit test (professor owner only)
# -----------------
@login_required
@require_http_methods(["GET", "POST"])
def test_detail(request, subject_id: int, test_id: int | None = None):
    subject = get_object_or_404(Subject, pk=subject_id)

    can_manage = _is_owner_prof(request.user, subject)
    # Only subject owner can create/edit
    if not can_manage:
        messages.error(request, "You are not allowed to edit tests for this subject.")
        return redirect("subjects:subject_detail", pk=subject.id)

    instance = None
    if test_id:
        instance = get_object_or_404(Test, pk=test_id, subject=subject, professor=request.user)

    if request.method == "POST":
        form = TestForm(request.POST, instance=instance)
        if form.is_valid():
            name_clean = form.cleaned_data["name"].strip()

            # unique (per professor+subject, case-insensitive)
            dup_qs = Test.objects.filter(
                subject=subject, professor=request.user, name__iexact=name_clean
            )
            if instance:
                dup_qs = dup_qs.exclude(pk=instance.pk)

            if dup_qs.exists():
                messages.error(request, "A test with this name already exists for this subject.")
            else:
                test = form.save(commit=False)
                test.subject = subject
                test.professor = request.user
                test.name = name_clean
                test.save()

                # If editing, reset questions for simplicity
                if instance:
                    instance.questions.all().delete()

                # Parse dynamic question blocks
                question_indices = set()
                for key in request.POST.keys():
                    if key.startswith("questions-") and key.endswith("-text"):
                        try:
                            qi = int(key.split("-")[1])
                            question_indices.add(qi)
                        except (ValueError, IndexError):
                            pass

                created_questions = 0
                for qi in sorted(question_indices):
                    q_text = (request.POST.get(f"questions-{qi}-text") or "").strip()
                    if not q_text:
                        continue
                    question = Question.objects.create(test=test, text=q_text)

                    # choices for this question
                    choice_indices = set()
                    prefix = f"questions-{qi}-choice-"
                    suffix = "-text"
                    for key in request.POST.keys():
                        if key.startswith(prefix) and key.endswith(suffix):
                            middle = key[len(prefix) : -len(suffix)]
                            try:
                                cj = int(middle)
                                choice_indices.add(cj)
                            except ValueError:
                                pass

                    # which index is correct?
                    correct_idx_raw = request.POST.get(f"questions-{qi}-correct")
                    try:
                        correct_idx = int(correct_idx_raw) if correct_idx_raw not in (None, "") else None
                    except (TypeError, ValueError):
                        correct_idx = None

                    made_any_choice = False
                    for cj in sorted(choice_indices):
                        c_text = (request.POST.get(f"questions-{qi}-choice-{cj}-text") or "").strip()
                        if not c_text:
                            continue
                        is_correct = (correct_idx == cj)
                        Choice.objects.create(question=question, text=c_text, is_correct=is_correct)
                        made_any_choice = True

                    # default first as correct if none marked
                    if made_any_choice and not question.choices.filter(is_correct=True).exists():
                        first = question.choices.first()
                        if first:
                            first.is_correct = True
                            first.save(update_fields=["is_correct"])

                    created_questions += 1

                messages.success(request, f"Test saved. ({created_questions} question(s))")
                return redirect("tests:my_tests", subject_id=subject.id)
        # fall-through to render with errors
    else:
        form = TestForm(instance=instance)

    # existing data (for JS prefill)
    existing = []
    if instance:
        for q in instance.questions.all().prefetch_related("choices"):
            existing.append(
                {
                    "id": q.id,
                    "text": q.text,
                    "choices": [
                        {"id": c.id, "text": c.text, "is_correct": bool(c.is_correct)}
                        for c in q.choices.all()
                    ],
                }
            )
    existing_json = json.dumps(existing)

    label = "Create Test" if not instance else f"Edit Test: {instance.name}"
    breadcrumbs = [
        ("Dashboard", reverse_lazy("users:dashboard")),
        ("My Subjects", reverse_lazy("subjects:my_subjects")),
        (f"Subject: {subject.name}", reverse_lazy("subjects:subject_detail", args=[subject.id])),
        ("Tests", reverse("tests:my_tests", kwargs={"subject_id": subject.id})),
        (label, ""),
    ]

    return render(
        request,
        "test_detail.html",
        {
            "subject": subject,
            "form": form,
            "test": instance,
            "existing_json": existing_json,
            "breadcrumbs": breadcrumbs,
            "can_manage": can_manage,   # important for template controls
        },
    )


# -----------------
# Student (and enrolled non-owner prof): take test only once
# -----------------
@login_required
def take_test(request, subject_id: int, test_id: int):
    subject = get_object_or_404(Subject, pk=subject_id)
    test = get_object_or_404(Test, pk=test_id, subject=subject)

    # Owner should edit, not take
    if _is_owner_prof(request.user, subject):
        messages.info(request, "You are the subject owner; use Edit Test instead.")
        return redirect("tests:edit_test", subject_id=subject.id, test_id=test.id)

    if not _is_enrolled(request.user, subject):
        messages.error(request, "You must be enrolled to take this test.")
        return redirect("tests:my_tests", subject_id=subject.id)

    # Prevent taking twice → redirect to review of their own attempt
    already = TestAttempt.objects.filter(test=test, student=request.user).first()
    if already:
        messages.info(request, "You have already taken this test. Opening your review.")
        return redirect("tests:test_attempt_detail", subject_id=subject.id, test_id=test.id, attempt_id=already.id)

    # Build questions->choices for display (template: take_test.html)
    questions = test.questions.all().prefetch_related("choices")

    breadcrumbs = [
        ("Dashboard", reverse_lazy("users:dashboard")),
        ("My Subjects", reverse_lazy("subjects:my_subjects")),
        (f"Subject: {subject.name}", reverse_lazy("subjects:subject_detail", args=[subject.id])),
        ("Tests", reverse("tests:my_tests", kwargs={"subject_id": subject.id})),
        (f"Take: {test.name}", ""),
    ]

    return render(
        request,
        "take_test.html",
        {
            "subject": subject,
            "test": test,
            "questions": questions,
            "breadcrumbs": breadcrumbs,
        },
    )


# -----------------
# Student submit attempt (enforce single attempt)
# -----------------
@login_required
@require_http_methods(["POST"])
def submit_attempt(request, subject_id: int, test_id: int):
    subject = get_object_or_404(Subject, pk=subject_id)
    test = get_object_or_404(Test, pk=test_id, subject=subject)

    # Enrolled or owner (owner path mostly for debugging)
    if not _is_enrolled(request.user, subject) and not _is_owner_prof(request.user, subject):
        messages.error(request, "You are not allowed to submit this test.")
        return redirect("tests:my_tests", subject_id=subject.id)

    # Enforce single attempt per user/test
    if TestAttempt.objects.filter(test=test, student=request.user).exists():
        messages.info(request, "You have already submitted this test.")
        return redirect("tests:my_tests", subject_id=subject.id)

    # Grade
    questions = list(test.questions.prefetch_related("choices"))
    max_score = len(questions)
    score = 0

    attempt = TestAttempt.objects.create(
        test=test,
        student=request.user,
        submitted_at=timezone.now(),
        max_score=max_score,
        score=0,
        duration_seconds=int(request.POST.get("elapsed_seconds", 0) or 0),
    )

    for q in questions:
        choice_id = request.POST.get(f"answer-{q.id}")
        if not choice_id:
            continue
        try:
            chosen = q.choices.get(pk=int(choice_id))
        except (ValueError, Choice.DoesNotExist):
            continue

        is_correct = bool(chosen.is_correct)
        AttemptAnswer.objects.create(
            attempt=attempt,
            question=q,
            choice=chosen,
            is_correct=is_correct,
        )
        if is_correct:
            score += 1

    attempt.score = score
    attempt.save(update_fields=["score"])

    pct = round((score * 100.0) / max_score, 2) if max_score else 0.0
    messages.success(request, f"Submitted! You scored {pct}% ({score}/{max_score}).")
    return redirect("tests:my_tests", subject_id=subject.id)


# -----------------
# Professor: submissions list
# -----------------
@login_required
def test_submissions(request, subject_id: int, test_id: int):
    subject = get_object_or_404(Subject, pk=subject_id)
    test = get_object_or_404(Test, pk=test_id, subject=subject)

    if not _is_owner_prof(request.user, subject):
        messages.error(request, "You are not allowed to view submissions for this test.")
        return redirect("tests:my_tests", subject_id=subject.id)

    attempts = (
        TestAttempt.objects.filter(test=test)
        .select_related("student")
        .prefetch_related("answers__question", "answers__choice")
        .order_by("-submitted_at")
    )

    # Precompute percentage to avoid custom template filters
    attempts = list(attempts)
    for a in attempts:
        a.pct = round((a.score * 100.0) / a.max_score, 2) if a.max_score else 0.0

    breadcrumbs = [
        ("Dashboard", reverse_lazy("users:dashboard")),
        ("My Subjects", reverse_lazy("subjects:my_subjects")),
        (f"Subject: {subject.name}", reverse_lazy("subjects:subject_detail", args=[subject.id])),
        ("Tests", reverse("tests:my_tests", kwargs={"subject_id": subject.id})),
        (f"Submissions: {test.name}", ""),
    ]

    return render(
        request,
        "test_submissions.html",
        {
            "subject": subject,
            "test": test,
            "attempts": attempts,
            "breadcrumbs": breadcrumbs,
        },
    )


# -----------------
# Attempt detail (professor or the student who owns the attempt)
# -----------------
@login_required
def test_attempt_detail(request, subject_id: int, test_id: int, attempt_id: int):
    subject = get_object_or_404(Subject, pk=subject_id)
    test = get_object_or_404(Test, pk=test_id, subject=subject)
    attempt = get_object_or_404(
        TestAttempt.objects.select_related("student").prefetch_related("answers__choice", "answers__question"),
        pk=attempt_id,
        test=test,
    )

    # Permission: owner professor OR the student who made this attempt
    is_owner_prof = _is_owner_prof(request.user, subject)
    is_attempt_owner = (attempt.student_id == request.user.id)

    if not (is_owner_prof or is_attempt_owner):
        messages.error(request, "You are not allowed to view this attempt.")
        return redirect("tests:my_tests", subject_id=subject.id)

    # Arrange rows question-by-question
    rows = []
    answers_by_qid = {a.question_id: a for a in attempt.answers.all()}
    for idx, q in enumerate(test.questions.all().prefetch_related("choices"), start=1):
        chosen = answers_by_qid.get(q.id)
        correct_choice = q.choices.filter(is_correct=True).first()
        rows.append(
            {
                "index": idx,
                "question": q,
                "chosen": chosen.choice if chosen else None,
                "is_correct": (chosen.is_correct if chosen else False),
                "correct_choice": correct_choice,
                "choices": list(q.choices.all()),
            }
        )

    # Percentage for header
    pct = round((attempt.score * 100.0) / attempt.max_score, 2) if attempt.max_score else 0.0

    # Breadcrumbs: tweak label if it’s the student’s own attempt
    label = (
        "Your Attempt"
        if is_attempt_owner and not is_owner_prof
        else f"Attempt by {attempt.student.get_full_name() or attempt.student.email}"
    )

    breadcrumbs = [
        ("Dashboard", reverse_lazy("users:dashboard")),
        ("My Subjects", reverse_lazy("subjects:my_subjects")),
        (f"Subject: {subject.name}", reverse_lazy("subjects:subject_detail", args=[subject.id])),
        ("Tests", reverse("tests:my_tests", kwargs={"subject_id": subject.id})),
        (label, ""),
    ]

    return render(
        request,
        "test_attempt_detail.html",
        {
            "subject": subject,
            "test": test,
            "attempt": attempt,
            "rows": rows,
            "breadcrumbs": breadcrumbs,
            "pct": pct,  # for (xx.xx%)
        },
    )


# -----------------
# Professor: analytics (with distribution + difficulty)
# -----------------
@login_required
def test_analytics(request, subject_id: int, test_id: int):
    subject = get_object_or_404(Subject, pk=subject_id)
    test = get_object_or_404(Test, pk=test_id, subject=subject)

    if not _is_owner_prof(request.user, subject):
        messages.error(request, "You are not allowed to view analytics for this test.")
        return redirect("tests:my_tests", subject_id=subject.id)

    attempts = TestAttempt.objects.filter(test=test).select_related("student")
    total_attempts = attempts.count()
    max_score = test.questions.count()

    # Defaults
    avg_score = None
    med_score = None
    best = None
    worst = None
    avg_pct = 0.0

    q_stats = []
    q_labels = []
    q_data = []

    if total_attempts and max_score:
        scores = list(attempts.values_list("score", flat=True))
        avg_score = (sum(scores) / total_attempts) if total_attempts else None
        med_score = median(scores) if total_attempts else None
        best = max(scores)
        worst = min(scores)
        avg_pct = (avg_score / max_score * 100.0) if avg_score is not None else 0.0

        # per-question difficulty
        question_stats = []
        for idx, q in enumerate(test.questions.all(), start=1):
            total_answers = AttemptAnswer.objects.filter(attempt__test=test, question=q).count()
            correct_answers = AttemptAnswer.objects.filter(attempt__test=test, question=q, is_correct=True).count()
            pct_correct = (correct_answers * 100.0 / total_answers) if total_answers else 0.0
            question_stats.append(
                {
                    "index": idx,
                    "id": q.id,
                    "text": q.text,
                    "total": total_answers,
                    "correct": correct_answers,
                    "pct": round(pct_correct, 2),
                }
            )
        # Sort by % correct ascending (hardest first); change to reverse=True for easiest first
        q_stats = sorted(question_stats, key=lambda x: x["pct"])

        # Data for chart.js (labels are Q1, Q2, …)
        q_labels = [f"Q{x['index']}" for x in q_stats]
        q_data = [x["pct"] for x in q_stats]

    # JSON for charts
    q_labels_json = json.dumps(q_labels)
    q_data_json = json.dumps(q_data)

    # Optional DB-side average % (not used by template, but kept for parity)
    avg_pct_db = attempts.aggregate(v=Avg(100.0 * F("score") / F("max_score")))["v"] if total_attempts else None
    if avg_pct_db is not None:
        avg_pct_db = round(avg_pct_db, 2)

    breadcrumbs = [
        ("Dashboard", reverse_lazy("users:dashboard")),
        ("My Subjects", reverse_lazy("subjects:my_subjects")),
        (f"Subject: {subject.name}", reverse_lazy("subjects:subject_detail", args=[subject.id])),
        ("Tests", reverse("tests:my_tests", kwargs={"subject_id": subject.id})),
        (f"Analytics: {test.name}", ""),
    ]

    return render(
        request,
        "test_analytics.html",
        {
            "subject": subject,
            "test": test,
            "total_attempts": total_attempts,
            "avg_score": avg_score,
            "avg_pct": round(avg_pct, 2) if isinstance(avg_pct, (int, float)) else 0.0,
            "avg_pct_db": avg_pct_db,
            "median_score": med_score,
            "best_score": best,
            "worst_score": worst,
            "max_score": max_score,
            # table + charts
            "q_stats": q_stats,
            "q_labels_json": q_labels_json,
            "q_data_json": q_data_json,
            "breadcrumbs": breadcrumbs,
        },
    )
