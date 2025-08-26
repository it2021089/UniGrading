# assignments/views.py
from django.contrib.auth.decorators import login_required
from django.contrib.auth.mixins import LoginRequiredMixin
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse_lazy
from django.utils import timezone
from django.views.decorators.http import require_POST
from django.views.generic import ListView, CreateView, DetailView, UpdateView

from UniGrading.mixin import BreadcrumbMixin
from subjects.models import Subject, Enrollment
from .models import Assignment
from .forms import AssignmentForm

DASHBOARD_URL = reverse_lazy("users:dashboard")

# Optional submissions model (graceful fallback if not present)
try:
    from .models import AssignmentSubmission  # expected: assignment(FK), student(FK), grade_pct (nullable)
    HAS_SUBMISSIONS = True
except Exception:
    AssignmentSubmission = None  # type: ignore
    HAS_SUBMISSIONS = False


def _is_owner_prof(user, subject: Subject) -> bool:
    return getattr(user, "role", None) == "professor" and getattr(subject, "professor_id", None) == user.id


def _is_enrolled(user, subject: Subject) -> bool:
    return Enrollment.objects.filter(user=user, subject=subject).exists()


class AssignmentListView(LoginRequiredMixin, BreadcrumbMixin, ListView):
    model = Assignment
    template_name = "assignment_list.html"
    context_object_name = "assignments"
    subject = None  # set in get_queryset

    def get_queryset(self):
        """Filter assignments by subject and ensure access rules."""
        self.subject = get_object_or_404(Subject, pk=self.kwargs["subject_id"])

        # Owner-prof always allowed; others must be enrolled.
        if not _is_owner_prof(self.request.user, self.subject) and not _is_enrolled(self.request.user, self.subject):
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
        assignments = list(ctx.get("assignments", []))
        user = self.request.user

        ctx["subject"] = self.subject
        ctx["can_manage"] = _is_owner_prof(user, self.subject)

        # Student-facing extras (submit/review/grade + deadline gating)
        if getattr(user, "role", None) != "professor" and assignments:
            now = timezone.now()

            # Defaults to avoid template errors
            for a in assignments:
                a.my_submission_id = None
                a.my_grade_pct = None
                a.can_submit = (a.due_date is None) or (a.due_date > now)

            if HAS_SUBMISSIONS:
                subs = {
                    s.assignment_id: s
                    for s in AssignmentSubmission.objects.filter(
                        assignment__in=assignments, student=user
                    ).only("id", "assignment_id", "grade_pct")
                }
                for a in assignments:
                    s = subs.get(a.id)
                    if s:
                        a.my_submission_id = s.id
                        a.my_grade_pct = getattr(s, "grade_pct", None)
                        # If already submitted, don't allow another submission from list
                        a.can_submit = False

        ctx["assignments"] = assignments
        return ctx


class AssignmentCreateView(LoginRequiredMixin, BreadcrumbMixin, CreateView):
    model = Assignment
    form_class = AssignmentForm
    template_name = "assignment_form.html"
    subject = None  # set in dispatch

    def dispatch(self, request, *args, **kwargs):
        self.subject = get_object_or_404(Subject, pk=kwargs["subject_id"])
        # Professors only (and must own the subject)
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
        assignment = self.get_object()
        return [
            ("Dashboard", DASHBOARD_URL),
            ("My Subjects", reverse_lazy("subjects:my_subjects")),
            (assignment.subject.name, reverse_lazy("subjects:subject_detail", kwargs={"pk": assignment.subject.pk})),
            ("Assignments", reverse_lazy("assignments:assignment_list", kwargs={"subject_id": assignment.subject.pk})),
            (assignment.title, self.request.path),
        ]

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        assignment = ctx["assignment"]
        subject = assignment.subject
        user = self.request.user

        ctx["subject"] = subject
        ctx["can_manage"] = _is_owner_prof(user, subject)

        # Student submission info + deadline gating for detail page
        if getattr(user, "role", None) != "professor":
            ctx["my_submission_id"] = None
            ctx["my_grade_pct"] = None
            ctx["can_submit"] = (assignment.due_date is None) or (assignment.due_date > timezone.now())

            if HAS_SUBMISSIONS:
                s = AssignmentSubmission.objects.filter(
                    assignment=assignment, student=user
                ).only("id", "grade_pct").first()
                if s:
                    ctx["my_submission_id"] = s.id
                    ctx["my_grade_pct"] = getattr(s, "grade_pct", None)
                    ctx["can_submit"] = False  # already submitted

        return ctx


@login_required
@require_POST
def delete_assignment(request, pk):
    assignment = get_object_or_404(Assignment, pk=pk)

    # Only the creating professor (or owner-professor of the subject) can delete
    if request.user != assignment.professor and not _is_owner_prof(request.user, assignment.subject):
        return redirect("assignments:assignment_list", subject_id=assignment.subject.pk)

    assignment.delete()
    return redirect("assignments:assignment_list", subject_id=assignment.subject.pk)


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
        assignment = self.get_object()
        subject = assignment.subject
        return [
            ("Dashboard", DASHBOARD_URL),
            ("My Subjects", reverse_lazy("subjects:my_subjects")),
            (subject.name, reverse_lazy("subjects:subject_detail", kwargs={"pk": subject.pk})),
            ("Assignments", reverse_lazy("assignments:assignment_list", kwargs={"subject_id": subject.pk})),
            (assignment.title, reverse_lazy("assignments:assignment_detail", kwargs={"pk": assignment.pk})),
            ("Edit", self.request.path),
        ]


# -----------------
# Stub routes to satisfy template links (real submit/review can be built later)
# -----------------

@login_required
def submit_assignment(request, pk: int):
    """
    Stub: redirect students to the assignment detail page (e.g., where the submit UI lives).
    Deadline gating is handled at the template and (ideally) in your real submit view/form too.
    """
    return redirect("assignments:assignment_detail", pk=pk)


@login_required
def submission_detail(request, pk: int):
    """
    Stub: if a submissions model exists, redirect to the related assignment detail;
    otherwise, go somewhere safe (dashboard).
    """
    if not HAS_SUBMISSIONS:
        return redirect(DASHBOARD_URL)

    sub = get_object_or_404(AssignmentSubmission, pk=pk)
    return redirect("assignments:assignment_detail", pk=sub.assignment_id)
