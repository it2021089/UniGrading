from django.shortcuts import get_object_or_404, redirect
from django.contrib.auth.decorators import login_required
from django.urls import reverse_lazy
from django.contrib.auth.mixins import LoginRequiredMixin
from django.views.generic import ListView, CreateView
from subjects.models import Subject
from .models import Assignment
from .forms import AssignmentForm
from django.views.generic import DetailView
from UniGrading.mixin import BreadcrumbMixin  
from django.views.generic import UpdateView
from django.views.decorators.http import require_POST


class AssignmentListView(LoginRequiredMixin, BreadcrumbMixin, ListView):
    model = Assignment
    template_name = "assignment_list.html"
    context_object_name = "assignments"

    def get_queryset(self):
        """Filter assignments by subject"""
        self.subject = get_object_or_404(Subject, pk=self.kwargs['subject_id'])
        return Assignment.objects.filter(subject=self.subject)

    def get_breadcrumbs(self):
        """Define breadcrumbs for the assignment list page"""
        return [
            ("Dashboard", reverse_lazy("users:professor_dashboard") if self.request.user.role == "professor" else reverse_lazy("users:student_dashboard")),
            ("My Subjects", reverse_lazy("subjects:my_subjects")),
            (self.subject.name, reverse_lazy("subjects:subject_detail", kwargs={"pk": self.subject.pk})),
            ("Assignments", self.request.path),  
        ]

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["subject"] = self.subject
        return context
class AssignmentCreateView(LoginRequiredMixin, BreadcrumbMixin, CreateView):
    model = Assignment
    form_class = AssignmentForm
    template_name = "assignment_form.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        subject = get_object_or_404(Subject, pk=self.kwargs["subject_id"])
        context["subject"] = subject
        return context

    def get_breadcrumbs(self):
        subject = get_object_or_404(Subject, pk=self.kwargs["subject_id"])
        return [
            ("Dashboard", reverse_lazy("users:professor_dashboard") if self.request.user.role == "professor" else reverse_lazy("users:student_dashboard")),
            ("My Subjects", reverse_lazy("subjects:my_subjects")),
            (subject.name, reverse_lazy("subjects:subject_detail", kwargs={"pk": subject.pk})),
            ("Assignments", reverse_lazy("assignments:assignment_list", kwargs={"subject_id": subject.pk})),
            ("Create Assignment", self.request.path),
        ]

    def form_valid(self, form):
        subject = get_object_or_404(Subject, pk=self.kwargs["subject_id"])
        form.instance.professor = self.request.user
        form.instance.subject = subject
        return super().form_valid(form)

    def get_success_url(self):
        return reverse_lazy("assignments:assignment_list", kwargs={"subject_id": self.kwargs["subject_id"]})


class AssignmentDetailView(LoginRequiredMixin, BreadcrumbMixin, DetailView):
    model = Assignment
    template_name = "assignment_detail.html"
    context_object_name = "assignment"

    def get_breadcrumbs(self):
        assignment = self.get_object()
        return [
            ("Dashboard", reverse_lazy("users:professor_dashboard") if self.request.user.role == "professor" else reverse_lazy("users:student_dashboard")),
            ("My Subjects", reverse_lazy("subjects:my_subjects")),
            (assignment.subject.name, reverse_lazy("subjects:subject_detail", kwargs={"pk": assignment.subject.pk})),
            ("Assignments", reverse_lazy("assignments:assignment_list", kwargs={"subject_id": assignment.subject.pk})),
            (assignment.title, self.request.path),
        ]
    

@login_required
@require_POST
def delete_assignment(request, pk):
    assignment = get_object_or_404(Assignment, pk=pk)
    if request.user != assignment.professor:
        return redirect('assignments:assignment_list', subject_id=assignment.subject.pk)
    
    assignment.delete()
    return redirect('assignments:assignment_list', subject_id=assignment.subject.pk)
class AssignmentUpdateView(LoginRequiredMixin, BreadcrumbMixin, UpdateView):
    model = Assignment
    form_class = AssignmentForm
    template_name = "assignment_form.html"

    def get_object(self):
        return get_object_or_404(Assignment, pk=self.kwargs['pk'])
    
    def get_success_url(self):
        return reverse_lazy('assignments:assignment_detail', kwargs={'pk': self.object.pk})

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["subject"] = self.object.subject
        return context

    def get_breadcrumbs(self):
        assignment = self.get_object()
        subject = assignment.subject

        dashboard_url = reverse_lazy("users:professor_dashboard") if self.request.user.role == "professor" else reverse_lazy("users:student_dashboard")

        return [
            ("Dashboard", dashboard_url),
            ("My Subjects", reverse_lazy("subjects:my_subjects")),
            (subject.name, reverse_lazy("subjects:subject_detail", kwargs={"pk": subject.pk})),
            ("Assignments", reverse_lazy("assignments:assignment_list", kwargs={"subject_id": subject.pk})),
            (assignment.title, reverse_lazy("assignments:assignment_detail", kwargs={"pk": assignment.pk})),
            ("Edit", self.request.path),
        ]