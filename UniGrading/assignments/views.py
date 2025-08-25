# assignments/views.py
from django.contrib.auth.decorators import login_required
from django.contrib.auth.mixins import LoginRequiredMixin
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse_lazy
from django.views.decorators.http import require_POST
from django.views.generic import ListView, CreateView, DetailView, UpdateView

from UniGrading.mixin import BreadcrumbMixin
from subjects.models import Subject
from .models import Assignment
from .forms import AssignmentForm


DASHBOARD_URL = reverse_lazy("users:dashboard")


class AssignmentListView(LoginRequiredMixin, BreadcrumbMixin, ListView):
    model = Assignment
    template_name = "assignment_list.html"
    context_object_name = "assignments"
    subject = None  # set in get_queryset

    def get_queryset(self):
        """Filter assignments by subject."""
        self.subject = get_object_or_404(Subject, pk=self.kwargs["subject_id"])
        return Assignment.objects.filter(subject=self.subject)

    def get_breadcrumbs(self):
        """Breadcrumbs for the assignment list page."""
        return [
            ("Dashboard", DASHBOARD_URL),
            ("My Subjects", reverse_lazy("subjects:my_subjects")),
            (self.subject.name, reverse_lazy("subjects:subject_detail", kwargs={"pk": self.subject.pk})),
            ("Assignments", self.request.path),
        ]

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["subject"] = self.subject
        return ctx


class AssignmentCreateView(LoginRequiredMixin, BreadcrumbMixin, CreateView):
    model = Assignment
    form_class = AssignmentForm
    template_name = "assignment_form.html"
    subject = None  # set in dispatch

    def dispatch(self, request, *args, **kwargs):
        self.subject = get_object_or_404(Subject, pk=kwargs["subject_id"])
        return super().dispatch(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["subject"] = self.subject
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


@login_required
@require_POST
def delete_assignment(request, pk):
    assignment = get_object_or_404(Assignment, pk=pk)

    # Keep your permission rule (only the creating professor can delete).
    if request.user != assignment.professor:
        return redirect("assignments:assignment_list", subject_id=assignment.subject.pk)

    assignment.delete()
    return redirect("assignments:assignment_list", subject_id=assignment.subject.pk)


class AssignmentUpdateView(LoginRequiredMixin, BreadcrumbMixin, UpdateView):
    model = Assignment
    form_class = AssignmentForm
    template_name = "assignment_form.html"

    def get_object(self):
        return get_object_or_404(Assignment, pk=self.kwargs["pk"])

    def get_success_url(self):
        return reverse_lazy("assignments:assignment_detail", kwargs={"pk": self.object.pk})

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["subject"] = self.object.subject
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
