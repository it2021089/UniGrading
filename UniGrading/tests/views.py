# tests/views.py
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse, reverse_lazy
from django.views.decorators.http import require_http_methods

from subjects.models import Subject
from .models import Test, Question, Choice
from .forms import TestForm

import json


@login_required
def my_tests(request, subject_id):
    """
    List tests for a given subject. Professors see their own tests.
    """
    subject = get_object_or_404(Subject, pk=subject_id)

    # Only the owning professor sees/manages tests here.
    if getattr(request.user, "role", None) == "professor" and subject.professor == request.user:
        tests = (
            Test.objects
            .filter(subject=subject, professor=request.user)
            .prefetch_related("questions")
            .order_by("-created_at")
        )
    else:
        tests = Test.objects.none()
        messages.error(request, "You are not allowed to view tests for this subject.")

    breadcrumbs = [
        ("Dashboard",
         reverse_lazy("users:dashboard") ),
        ("My Subjects", reverse_lazy("subjects:my_subjects")),
        (f"Subject: {subject.name}", reverse_lazy("subjects:subject_detail", args=[subject.id])),
        ("Tests", ""),  # current page
    ]

    return render(request, "my_tests.html", {
        "subject": subject,
        "tests": tests,
        "breadcrumbs": breadcrumbs,
    })


@login_required
@require_http_methods(["GET", "POST"])
def test_detail(request, subject_id, test_id=None):
    """
    Create / Edit a test and its MCQ questions.

    POST keys produced by the dynamic form:
      - name
      - duration_minutes
      - For each question index i:
          questions-{i}-text
          questions-{i}-choice-{j}-text    (for each choice j)
          questions-{i}-correct            (value j, the index of the correct choice)
    """
    subject = get_object_or_404(Subject, pk=subject_id)

    # Permission: only the professor who owns the subject can manage tests
    if getattr(request.user, "role", None) != "professor" or subject.professor != request.user:
        messages.error(request, "You are not allowed to edit tests for this subject.")
        return redirect("subjects:subject_detail", pk=subject.id)

    instance = None
    if test_id:
        instance = get_object_or_404(Test, pk=test_id, subject=subject, professor=request.user)

    if request.method == "POST":
        form = TestForm(request.POST, instance=instance)
        if form.is_valid():
            name_clean = form.cleaned_data["name"].strip()

            # Prevent duplicate names per (subject, professor) ignoring case
            dup_qs = Test.objects.filter(
                subject=subject,
                professor=request.user,
                name__iexact=name_clean
            )
            if instance:
                dup_qs = dup_qs.exclude(pk=instance.pk)

            if dup_qs.exists():
                messages.error(request, "A test with this name already exists for this subject.")
            else:
                # Save test instance
                test = form.save(commit=False)
                test.subject = subject
                test.professor = request.user
                test.name = name_clean
                test.save()

                # If editing, clear previous tree for simplicity
                if instance:
                    instance.questions.all().delete()

                # ---- Parse questions from POST ----
                # Collect question indices by scanning keys like 'questions-0-text'
                question_indices = set()
                for key in request.POST.keys():
                    if key.startswith("questions-") and key.endswith("-text"):
                        # key format: questions-{i}-text
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

                    # Collect choices for this question
                    choice_indices = set()
                    prefix = f"questions-{qi}-choice-"
                    suffix = "-text"
                    for key in request.POST.keys():
                        if key.startswith(prefix) and key.endswith(suffix):
                            middle = key[len(prefix):-len(suffix)]
                            try:
                                cj = int(middle)
                                choice_indices.add(cj)
                            except ValueError:
                                pass

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

                    # If no choice marked correct but choices exist, default first one as correct
                    if made_any_choice and not question.choices.filter(is_correct=True).exists():
                        first = question.choices.first()
                        if first:
                            first.is_correct = True
                            first.save(update_fields=["is_correct"])

                    created_questions += 1

                messages.success(request, f"Test saved. ({created_questions} question(s))")
                return redirect("tests:my_tests", subject_id=subject.id)
        # If invalid, fall through to render form with errors
    else:
        form = TestForm(instance=instance)

    # Build existing questions/choices for prefill (serialize to JSON for the template JS)
    existing = []
    if instance:
        for q in instance.questions.all().prefetch_related("choices"):
            existing.append({
                "id": q.id,
                "text": q.text,
                "choices": [
                    {"id": c.id, "text": c.text, "is_correct": bool(c.is_correct)}
                    for c in q.choices.all()
                ],
            })
    existing_json = json.dumps(existing)

    current_label = "Create Test" if not instance else f"Edit Test: {instance.name}"
    breadcrumbs = [
        ("Dashboard", reverse_lazy("users:dashboard")),
        ("My Subjects", reverse_lazy("subjects:my_subjects")),
        (f"Subject: {subject.name}", reverse_lazy("subjects:subject_detail", args=[subject.id])),
        ("Tests", reverse("tests:my_tests", kwargs={"subject_id": subject.id})),
        (current_label, ""),
    ]

    return render(request, "test_detail.html", {
        "subject": subject,
        "form": form,
        "test": instance,
        "existing_json": existing_json,   
        "breadcrumbs": breadcrumbs,
    })
