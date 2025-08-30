# assignments/views.py
from pathlib import Path
import logging
import mimetypes

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.files.storage import default_storage
from django.http import Http404, FileResponse
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse_lazy, reverse
from django.utils import timezone
from django.views.decorators.clickjacking import xframe_options_exempt
from django.views.generic import ListView, CreateView, DetailView, UpdateView

from UniGrading.mixin import BreadcrumbMixin
from subjects.models import Subject, Enrollment
from .forms import AssignmentForm
from .models import Assignment

logger = logging.getLogger(__name__)
DASHBOARD_URL = reverse_lazy("users:dashboard")

# Optional submissions model (enable if present)
try:
    from .models import AssignmentSubmission  # type: ignore
    HAS_SUBMISSIONS = True
except Exception:
    AssignmentSubmission = None  # type: ignore
    HAS_SUBMISSIONS = False

# Autograder
try:
    from .autograder import grade_submission, apply_result_to_submission  # type: ignore
    HAS_AUTOGRADER = True
except Exception as _e:
    logger.warning("Autograder unavailable: %s", _e)
    HAS_AUTOGRADER = False


# -----------------------
# Permission helpers
# -----------------------
def _is_owner_prof(user, subject: Subject) -> bool:
    return getattr(user, "role", None) == "professor" and subject.professor_id == user.id


def _is_enrolled(user, subject: Subject) -> bool:
    return Enrollment.objects.filter(user=user, subject=subject).exists()


# -----------------------
# Views
# -----------------------
class AssignmentListView(LoginRequiredMixin, BreadcrumbMixin, ListView):
    model = Assignment
    template_name = "assignment_list.html"
    context_object_name = "assignments"
    subject: Subject | None = None  # set in get_queryset

    def get_queryset(self):
        self.subject = get_object_or_404(Subject, pk=self.kwargs["subject_id"])

        # Only subject owner or enrolled users can see the list
        if not (_is_owner_prof(self.request.user, self.subject) or _is_enrolled(self.request.user, self.subject)):
            return Assignment.objects.none()

        return (
            Assignment.objects.filter(subject=self.subject)
            .select_related("professor")
            .order_by("-due_date", "title")
        )

    def get_breadcrumbs(self):
        return [
            ("Dashboard", DASHBOARD_URL),
            ("My Subjects", reverse_lazy("subjects:my_subjects")),
            (self.subject.name, reverse_lazy("subjects:subject_detail", kwargs={"pk": self.subject.pk})),
            ("Assignments", self.request.path),
        ]

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        user = self.request.user
        ctx["subject"] = self.subject
        ctx["can_manage"] = _is_owner_prof(user, self.subject)
        ctx["breadcrumbs"] = self.get_breadcrumbs()

        # student convenience flags
        if getattr(user, "role", None) != "professor":
            now = timezone.now()
            for a in ctx["assignments"]:
                a.can_submit = a.due_date > now
                a.my_submission_id = None
                a.my_grade_pct = None

            if HAS_SUBMISSIONS and ctx["assignments"]:
                subs = {
                    s.assignment_id: s
                    for s in AssignmentSubmission.objects.filter(
                        assignment__in=ctx["assignments"], student=user
                    ).only("id", "assignment_id", "grade_pct")
                }
                for a in ctx["assignments"]:
                    s = subs.get(a.id)
                    if s:
                        a.my_submission_id = s.id
                        a.my_grade_pct = getattr(s, "grade_pct", None)
                        a.can_submit = a.due_date > now  # can still re-submit before deadline

        return ctx


class AssignmentCreateView(LoginRequiredMixin, BreadcrumbMixin, CreateView):
    model = Assignment
    form_class = AssignmentForm
    template_name = "assignment_form.html"
    subject: Subject | None = None

    def dispatch(self, request, *args, **kwargs):
        self.subject = get_object_or_404(Subject, pk=kwargs["subject_id"])
        if not _is_owner_prof(request.user, self.subject):
            return redirect("assignments:assignment_list", subject_id=self.subject.pk)
        return super().dispatch(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["subject"] = self.subject
        ctx["can_manage"] = True
        return ctx

    def get_breadcrumbs(self):
        return [
            ("Dashboard", DASHBOARD_URL),
            ("My Subjects", reverse_lazy("subjects:my_subjects")),
            (self.subject.name, reverse_lazy("subjects:subject_detail", kwargs={"pk": self.subject.pk})),
            ("Assignments", reverse_lazy("assignments:assignment_list", kwargs={"subject_id": self.subject.pk})),
            ("Create Assignment", self.request.path),
        ]

    def form_valid(self, form):
        form.instance.professor = self.request.user
        form.instance.subject = self.subject
        return super().form_valid(form)

    def get_success_url(self):
        return reverse_lazy("assignments:assignment_list", kwargs={"subject_id": self.subject.pk})


class AssignmentDetailView(LoginRequiredMixin, BreadcrumbMixin, DetailView):
    model = Assignment
    template_name = "assignment_detail.html"
    context_object_name = "assignment"

    def get_breadcrumbs(self):
        a = self.get_object()
        return [
            ("Dashboard", DASHBOARD_URL),
            ("My Subjects", reverse_lazy("subjects:my_subjects")),
            (a.subject.name, reverse_lazy("subjects:subject_detail", kwargs={"pk": a.subject.pk})),
            ("Assignments", reverse_lazy("assignments:assignment_list", kwargs={"subject_id": a.subject.pk})),
            (a.title, self.request.path),
        ]

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        a: Assignment = ctx["assignment"]
        u = self.request.user

        ctx["subject"] = a.subject
        ctx["can_manage"] = _is_owner_prof(u, a.subject)
        ctx["is_enrolled"] = _is_enrolled(u, a.subject)
        ctx["breadcrumbs"] = self.get_breadcrumbs()

        if a.file:
            ctx["file_basename"] = Path(a.file.name).name
            ctx["preview_url"] = reverse("assignments:preview_assignment_file", kwargs={"pk": a.pk})
            ctx["download_url"] = reverse("assignments:download_assignment_file", kwargs={"pk": a.pk})

        # Submission info (students & non-owner profs)
        is_owner = ctx["can_manage"]
        if (getattr(u, "role", None) != "professor") or (getattr(u, "role", None) == "professor" and not is_owner):
            ctx["my_submission_id"] = None
            ctx["my_grade_pct"] = None
            ctx["my_submission_feedback"] = None
            ctx["can_submit"] = a.due_date > timezone.now() and ctx["is_enrolled"]
            if HAS_SUBMISSIONS:
                s = AssignmentSubmission.objects.filter(assignment=a, student=u).only(
                    "id", "grade_pct", "submitted_at", "file", "ai_feedback"
                ).first()
                if s:
                    ctx["my_submission_id"] = s.id
                    ctx["my_grade_pct"] = getattr(s, "grade_pct", None)
                    ctx["my_submission_feedback"] = getattr(s, "ai_feedback", None)
                    ctx["my_submission_filename"] = (
                        Path(getattr(s.file, "name", "")).name if getattr(s, "file", None) else ""
                    )
                    ctx["my_submission_uploaded_at"] = getattr(s, "submitted_at", None)
                    ctx["can_submit"] = a.due_date > timezone.now() and ctx["is_enrolled"]
                    ctx["my_submission_preview_url"] = reverse(
                        "assignments:preview_submission_file", kwargs={"pk": s.id}
                    )
                    ctx["my_submission_download_url"] = reverse(
                        "assignments:download_submission_file", kwargs={"pk": s.id}
                    )

        return ctx


class AssignmentUpdateView(LoginRequiredMixin, BreadcrumbMixin, UpdateView):
    model = Assignment
    form_class = AssignmentForm
    template_name = "assignment_form.html"

    def get_object(self):
        return get_object_or_404(Assignment, pk=self.kwargs["pk"])

    def dispatch(self, request, *args, **kwargs):
        obj = self.get_object()
        if not _is_owner_prof(request.user, obj.subject):
            return redirect("assignments:assignment_detail", pk=obj.pk)
        return super().dispatch(request, *args, **kwargs)

    def get_success_url(self):
        return reverse_lazy("assignments:assignment_detail", kwargs={"pk": self.object.pk})

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["subject"] = self.object.subject
        ctx["can_manage"] = True
        return ctx

    def get_breadcrumbs(self):
        a = self.get_object()
        s = a.subject
        return [
            ("Dashboard", DASHBOARD_URL),
            ("My Subjects", reverse_lazy("subjects:my_subjects")),
            (s.name, reverse_lazy("subjects:subject_detail", kwargs={"pk": s.pk})),
            ("Assignments", reverse_lazy("assignments:assignment_list", kwargs={"subject_id": s.pk})),
            (a.title, reverse_lazy("assignments:assignment_detail", kwargs={"pk": a.pk})),
            ("Edit", self.request.path),
        ]


@login_required
def delete_assignment(request, pk):
    if request.method != "POST":
        return redirect("assignments:assignment_detail", pk=pk)

    assignment = get_object_or_404(Assignment, pk=pk)
    if request.user != assignment.professor and not _is_owner_prof(request.user, assignment.subject):
        return redirect("assignments:assignment_list", subject_id=assignment.subject.pk)
    assignment.delete()
    return redirect("assignments:assignment_list", subject_id=assignment.subject.pk)


# -----------------------
# File streaming helpers
# -----------------------
def _open_from_bound_storage(file_field, key: str):
    """Try the storage bound to the FileField first."""
    try:
        storage = file_field.storage
        logger.info("Assignment file request: storage=%s key=%s", storage.__class__.__name__, key)
        return storage.open(key, "rb")
    except Exception as e:
        logger.warning("Bound storage open failed for key=%s: %s", key, e)
        return None


def _open_from_default_storage(key: str):
    """Fallback to default_storage (whatever STORAGES['default'] points to)."""
    try:
        logger.info(
            "Assignment file request (fallback default_storage): storage=%s key=%s",
            default_storage.__class__.__name__, key
        )
        return default_storage.open(key, "rb")
    except Exception as e:
        logger.warning("Default storage open failed for key=%s: %s", key, e)
        return None


def _stream_file_for_assignment(request, assignment: Assignment, inline: bool):
    """Unified streaming for assignment.file with robust storage fallback."""
    subj = assignment.subject
    if not (_is_owner_prof(request.user, subj) or _is_enrolled(request.user, subj) or request.user.is_superuser):
        raise Http404("Not found.")

    if not assignment.file:
        raise Http404("File not accessible.")

    key = assignment.file.name
    filename = Path(key).name
    content_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"

    fh = _open_from_bound_storage(assignment.file, key) or _open_from_default_storage(key)
    if not fh:
        logger.error("Failed to open assignment file key=%s (inline=%s)", key, inline)
        raise Http404("File not accessible.")

    resp = FileResponse(fh, content_type=content_type)
    disp = "inline" if inline else "attachment"
    resp["Content-Disposition"] = f'{disp}; filename="{filename}"'
    return resp


# -----------------------
# Assignment file preview/download
# -----------------------
@xframe_options_exempt
@login_required
def preview_assignment_file(request, pk: int):
    a = get_object_or_404(Assignment, pk=pk)
    return _stream_file_for_assignment(request, a, inline=True)


@login_required
def download_assignment_file(request, pk: int):
    a = get_object_or_404(Assignment, pk=pk)
    return _stream_file_for_assignment(request, a, inline=False)


# -----------------------
# Submissions
# -----------------------
@login_required
def submit_assignment(request, pk: int):
    """
    Create or replace the current user's submission for this assignment.
    Allowed for enrolled students AND non-owner enrolled professors.
    The owning professor cannot submit (unless superuser).
    """
    a = get_object_or_404(Assignment, pk=pk)

    # Feature toggle
    if not HAS_SUBMISSIONS or AssignmentSubmission is None:
        messages.error(request, "Submissions are not enabled.")
        return redirect("assignments:assignment_detail", pk=a.pk)

    # Permissions
    is_owner = _is_owner_prof(request.user, a.subject)
    is_enrolled = _is_enrolled(request.user, a.subject)
    if is_owner and not request.user.is_superuser:
        messages.error(request, "You cannot submit to your own assignment.")
        return redirect("assignments:assignment_detail", pk=a.pk)
    if not (is_enrolled or request.user.is_superuser):
        raise Http404("Not found.")

    if request.method != "POST":
        return redirect("assignments:assignment_detail", pk=a.pk)

    # Deadline
    if a.due_date <= timezone.now():
        messages.error(request, "Deadline has passed — submissions are closed.")
        return redirect("assignments:assignment_detail", pk=a.pk)

    upfile = request.FILES.get("file")
    if not upfile:
        messages.error(request, "Please choose a file to upload.")
        return redirect("assignments:assignment_detail", pk=a.pk)

    # Create or replace user's submission
    sub = AssignmentSubmission.objects.filter(assignment=a, student=request.user).first()
    if sub:
        try:
            if getattr(sub, "file", None) and sub.file.name:
                sub.file.delete(save=False)
        except Exception as e:
            logger.info("Old submission file delete skipped: %s", e)
        sub.file = upfile
        sub.submitted_at = timezone.now()
        if hasattr(sub, "autograde_status"):
            sub.autograde_status = "queued"
        if hasattr(sub, "ai_feedback"):
            sub.ai_feedback = ""
        if hasattr(sub, "runner_logs"):
            sub.runner_logs = ""
        sub.save()
        messages.success(request, "Submission updated.")
    else:
        sub = AssignmentSubmission.objects.create(
            assignment=a,
            student=request.user,
            file=upfile,
            submitted_at=timezone.now(),
            **({"autograde_status": "queued"} if "autograde_status" in [f.name for f in AssignmentSubmission._meta.fields] else {})
        )
        messages.success(request, "Submission uploaded.")

    # ---- Autograde now (sync) ----
    if HAS_AUTOGRADER:
        try:
            result = grade_submission(assignment=a, submission=sub)
            apply_result_to_submission(sub, result)
            sub.save()
            status = result.get("status")
            if status == "done" and result.get("grade_pct") is not None:
                messages.success(request, f"Auto-graded: {result.get('grade_pct', 0):.2f}%")
            else:
                messages.warning(request, "Automatic grading unavailable; your submission awaits manual review.")
        except Exception as e:
            logger.exception("Autograder crashed: %s", e)
            if hasattr(sub, "autograde_status"):
                sub.autograde_status = "failed"
                sub.save(update_fields=["autograde_status"])
            messages.warning(request, "Autograder failed. Your file is saved for manual review.")

    return redirect("assignments:assignment_detail", pk=a.pk)


def _stream_submission_file(request, submission, inline: bool):
    """
    Stream a submission's file. Allowed to owner professor, the submitter, or superuser.
    """
    a = submission.assignment

    allowed = (
        request.user.is_superuser
        or _is_owner_prof(request.user, a.subject)
        or submission.student_id == request.user.id
    )
    if not allowed:
        raise Http404("Not found.")

    if not submission.file:
        raise Http404("File not accessible.")

    key = submission.file.name
    filename = Path(key).name
    content_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"

    try:
        fh = default_storage.open(key, "rb")
    except Exception as e:
        logger.warning("Submission open failed key=%s: %s", key, e)
        raise Http404("File not accessible.")

    resp = FileResponse(fh, content_type=content_type)
    disp = "inline" if inline else "attachment"
    resp["Content-Disposition"] = f'{disp}; filename="{filename}"'
    return resp


@xframe_options_exempt
@login_required
def preview_submission_file(request, pk: int):
    if not HAS_SUBMISSIONS or AssignmentSubmission is None:
        raise Http404("Not found.")
    s = get_object_or_404(AssignmentSubmission, pk=pk)
    return _stream_submission_file(request, s, inline=True)


@login_required
def download_submission_file(request, pk: int):
    if not HAS_SUBMISSIONS or AssignmentSubmission is None:
        raise Http404("Not found.")
    s = get_object_or_404(AssignmentSubmission, pk=pk)
    return _stream_submission_file(request, s, inline=False)
