# assignments/views.py
from pathlib import Path
import logging
import mimetypes
from urllib.parse import quote as urlquote

from django.core.files.storage import default_storage
from django.contrib.auth.decorators import login_required
from django.contrib.auth.mixins import LoginRequiredMixin
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

# Optional submissions model
try:
    from .models import AssignmentSubmission  # type: ignore
    HAS_SUBMISSIONS = True
except Exception:
    AssignmentSubmission = None  # type: ignore
    HAS_SUBMISSIONS = False


# --------------------------
# Helpers
# --------------------------
def _is_owner_prof(user, subject: Subject) -> bool:
    return getattr(user, "role", None) == "professor" and subject.professor_id == getattr(user, "id", None)


def _is_enrolled(user, subject: Subject) -> bool:
    return Enrollment.objects.filter(user=user, subject=subject).exists()


def _can_view_file(user, assignment: Assignment) -> bool:
    if getattr(user, "is_superuser", False):
        return True
    if getattr(user, "role", None) == "professor" and assignment.professor_id == getattr(user, "id", None):
        return True
    return Enrollment.objects.filter(user=user, subject_id=assignment.subject_id).exists()


def _guess_content_type(name: str) -> str:
    return mimetypes.guess_type(name)[0] or "application/octet-stream"


def _stream_file(assignment: Assignment, *, inline: bool) -> FileResponse:
    """
    Open by key using Django's default_storage so we always hit the
    active S3Boto3/MinIO backend, even if the field's storage differs.
    """
    if not assignment.file:
        raise Http404("No file attached.")

    key = assignment.file.name
    filename = Path(key).name
    content_type = _guess_content_type(filename)

    try:
        fh = default_storage.open(key, "rb")
    except Exception as e:
        logger.exception("Failed to open assignment file via default_storage key=%s: %s", key, e)
        raise Http404("File not found.")

    # RFC 5987 for non-ASCII filenames; keep legacy filename for compatibility
    disp_base = "inline" if inline else "attachment"
    content_disp = f'{disp_base}; filename="{filename}"; filename*=UTF-8\'\'{urlquote(filename)}'

    resp = FileResponse(fh, content_type=content_type)
    resp["Content-Disposition"] = content_disp
    return resp


# --------------------------
# Views
# --------------------------
class AssignmentListView(LoginRequiredMixin, BreadcrumbMixin, ListView):
    model = Assignment
    template_name = "assignment_list.html"
    context_object_name = "assignments"
    subject = None  # set in get_queryset

    def get_queryset(self):
        self.subject = get_object_or_404(Subject, pk=self.kwargs["subject_id"])

        # owner or enrolled
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

        # student conveniences
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
                        a.can_submit = False

        return ctx


class AssignmentCreateView(LoginRequiredMixin, BreadcrumbMixin, CreateView):
    model = Assignment
    form_class = AssignmentForm
    template_name = "assignment_form.html"
    subject = None

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

        # file endpoints for template buttons
        if a.file:
            ctx["file_basename"] = Path(a.file.name).name  # or a.file_basename property
            ctx["preview_url"] = reverse("assignments:preview_assignment_file", kwargs={"pk": a.pk})
            ctx["download_url"] = reverse("assignments:download_assignment_file", kwargs={"pk": a.pk})

        # student submission info
        if getattr(u, "role", None) != "professor":
            ctx["my_submission_id"] = None
            ctx["my_grade_pct"] = None
            ctx["can_submit"] = a.due_date > timezone.now()
            if HAS_SUBMISSIONS:
                s = AssignmentSubmission.objects.filter(assignment=a, student=u).only("id", "grade_pct").first()
                if s:
                    ctx["my_submission_id"] = s.id
                    ctx["my_grade_pct"] = getattr(s, "grade_pct", None)
                    ctx["can_submit"] = False

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
        if self.object.file:
            ctx["current_file_basename"] = Path(self.object.file.name).name
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
    subject_id = assignment.subject.pk
    assignment.delete()
    return redirect("assignments:assignment_list", subject_id=subject_id)


# --------------------------
# File endpoints (stream via default_storage)
# --------------------------
@xframe_options_exempt  # allow embedding PDFs/images in <iframe>
@login_required
def preview_assignment_file(request, pk: int):
    a = get_object_or_404(Assignment, pk=pk)
    if not _can_view_file(request.user, a):
        raise Http404("Not found.")
    return _stream_file(a, inline=True)


@login_required
def download_assignment_file(request, pk: int):
    a = get_object_or_404(Assignment, pk=pk)
    if not _can_view_file(request.user, a):
        raise Http404("Not found.")
    return _stream_file(a, inline=False)
