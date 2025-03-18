from django.shortcuts import get_object_or_404
from django.urls import reverse_lazy
from django.contrib.auth.mixins import LoginRequiredMixin
from django.views.generic import ListView, CreateView
from subjects.models import Subject
from .models import Assignment
from .forms import AssignmentForm
from UniGrading.mixin import BreadcrumbMixin  # Import BreadcrumbMixin

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
        context["subject"] = subject  # Ensure subject is passed to the template
        return context

    def form_valid(self, form):
        subject = get_object_or_404(Subject, pk=self.kwargs["subject_id"])
        form.instance.professor = self.request.user
        form.instance.subject = subject  # Ensure the assignment is linked to the correct subject
        return super().form_valid(form)

    def get_success_url(self):
        return reverse_lazy("assignments:assignment_list", kwargs={"subject_id": self.kwargs["subject_id"]})
