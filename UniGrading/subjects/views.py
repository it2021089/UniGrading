from django.shortcuts import render, get_object_or_404, redirect
from django.http import JsonResponse
from django.contrib.auth.decorators import login_required
from django.views.decorators.csrf import csrf_exempt
from django.views.generic import DetailView, ListView, CreateView
from django.contrib.auth.mixins import LoginRequiredMixin
from django.views.decorators.http import require_POST
from django.urls import reverse_lazy
from .models import Subject, Category, File
from .forms import SubjectForm
from UniGrading.mixin import BreadcrumbMixin
from django.db import transaction, IntegrityError
from django.contrib import messages 
import logging

# Set up logging
logger = logging.getLogger(__name__)

# --------------------------
# My Subjects View
# --------------------------
class MySubjectsView(LoginRequiredMixin, BreadcrumbMixin, ListView):
    model = Subject
    template_name = "my_subjects.html"
    context_object_name = "subjects"
    paginate_by = 6

    def get_breadcrumbs(self):
        dashboard_url = reverse_lazy("users:login")
        if self.request.user.role == "professor":
            dashboard_url = reverse_lazy("users:professor_dashboard")
        elif self.request.user.role == "student":
            dashboard_url = reverse_lazy("users:student_dashboard")

        return [
            ("Dashboard", dashboard_url),
            ("My Subjects", reverse_lazy("subjects:my_subjects")),
        ]

    def post(self, request, *args, **kwargs):
        return self.get(request, *args, **kwargs)

# --------------------------
# Create Subject View
# --------------------------
class CreateSubjectView(LoginRequiredMixin, CreateView):
    model = Subject
    form_class = SubjectForm
    template_name = "create_subjects.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        dashboard_url = reverse_lazy("users:login")
        if self.request.user.role == "professor":
            dashboard_url = reverse_lazy("users:professor_dashboard")
        elif self.request.user.role == "student":
            dashboard_url = reverse_lazy("users:student_dashboard")

        context['breadcrumbs'] = [
            ("Dashboard", dashboard_url),
            ("My Subjects", reverse_lazy("subjects:my_subjects")),
            ("Create Subject", "")
        ]
        return context

    def form_valid(self, form):
        subject = form.save(commit=False)
        subject.professor = self.request.user

        with transaction.atomic():
            subject.save()

            # Default categories
            default_categories = ["Courses", "Assignments", "Tests", "Other"]
            for category_name in default_categories:
                Category.objects.get_or_create(subject=subject, name=category_name, parent=None)

            # Additional categories from POST request
            additional_categories = self.request.POST.getlist('categories')
            for category_name in additional_categories:
                category_name = category_name.strip()
                if category_name:
                    Category.objects.get_or_create(subject=subject, name=category_name, parent=None)

        return super().form_valid(form)

    def get_success_url(self):
        return reverse_lazy("subjects:my_subjects")

# --------------------------
# Subject Detail View
# --------------------------
class SubjectDetailView(LoginRequiredMixin, BreadcrumbMixin, DetailView):
    model = Subject
    template_name = "subject_detail.html"
    context_object_name = "subject"

    def get_breadcrumbs(self):
        dashboard_url = reverse_lazy("users:login")
        if self.request.user.role == "professor":
            dashboard_url = reverse_lazy("users:professor_dashboard")
        elif self.request.user.role == "student":
            dashboard_url = reverse_lazy("users:student_dashboard")

        return [
            ("Dashboard", dashboard_url),
            ("My Subjects", reverse_lazy("subjects:my_subjects")),
            (f"Subject: {self.object.name}", self.request.path),
        ]

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["categories"] = self.object.categories.filter(parent__isnull=True)
        context["default_categories"] = ["Courses", "Assignments", "Tests", "Other"] 
        return context

    def post(self, request, *args, **kwargs):
        subject = self.get_object()
        data = request.POST

        if "description" in data:
            subject.description = data.get("description", "").strip()
            subject.save()
            return JsonResponse({"status": "success", "message": "Description updated!", "new_description": subject.description})

        elif "new_category" in data:
            category_name = data.get("new_category", "").strip()
            if category_name:
                category = Category.objects.create(subject=subject, name=category_name, parent=None)
                return JsonResponse({"status": "success", "message": "Category added!", "category_name": category.name, "category_id": category.id})

        elif "rename_category" in data:
            category_id = data.get("category_id")
            category_name = data.get("category_name", "").strip()
            category = get_object_or_404(Category, id=category_id)
            category.name = category_name
            category.save()
            return JsonResponse({"status": "success", "message": "Category renamed!", "new_name": category.name})
        
        elif "delete_category" in data:
            category_id = data.get("category_id")
            category = get_object_or_404(Category, id=category_id)
            category.delete()
            return JsonResponse({"status": "success", "message": "Category deleted!"})

        return JsonResponse({"status": "error", "message": "Invalid request."})

# --------------------------
# Category Detail View
# --------------------------
class CategoryDetailView(LoginRequiredMixin, BreadcrumbMixin, DetailView):
    model = Category
    template_name = "category_detail.html"
    context_object_name = "category"
    def get_breadcrumbs(self):
        category = self.get_object()

        # Determine correct dashboard URL
        dashboard_url = reverse_lazy("users:login")
        if self.request.user.role == "professor":
            dashboard_url = reverse_lazy("users:professor_dashboard")
        elif self.request.user.role == "student":
            dashboard_url = reverse_lazy("users:student_dashboard")

        # Build breadcrumbs
        breadcrumbs = [
            ("Dashboard", dashboard_url),
            ("My Subjects", reverse_lazy("subjects:my_subjects")),
            (f"Subject: {category.subject.name}", reverse_lazy("subjects:subject_detail", args=[category.subject.pk])),
            (f"Category: {category.name}", self.request.path),
        ]

        return breadcrumbs

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["breadcrumbs"] = self.get_breadcrumbs()  # Ensure breadcrumbs are added to context
        context["subcategories"] = self.object.subcategories.all()
        context["files"] = self.object.files.all()
        return context
    def post(self, request, *args, **kwargs):
        category = self.get_object()
        data = request.POST
        
        if "new_subcategory" in data:
            subcategory_name = data.get("new_subcategory", "").strip()
            if subcategory_name:
                Category.objects.create(subject=category.subject, name=subcategory_name, parent=category)
                messages.success(request, "Subcategory added successfully!")  

        elif "new_file" in request.POST:
            if "file" not in request.FILES:
                messages.error(request, "No file provided.")
                return redirect(self.request.path)

            uploaded_file = request.FILES["file"]

            try:
                File.objects.create(category=category, name=uploaded_file.name, file=uploaded_file)
                messages.success(request, "File uploaded successfully!")  # Success message
            except Exception as e:
                messages.error(request, f"Upload failed: {str(e)}")  # Error message

        return redirect(self.request.path) 

# --------------------------
# Delete Subject (Function-Based View)
# --------------------------
@login_required
@require_POST
def delete_subject(request, pk):
    subject = get_object_or_404(Subject, pk=pk)

    if request.user != subject.professor:
        return redirect("subjects:my_subjects")

    subject.delete()
    return redirect("subjects:my_subjects")

# --------------------------
# Delete File (Function-Based View)
# --------------------------
@login_required
def delete_file(request, pk):
    file = get_object_or_404(File, pk=pk)
    category_pk = file.category.pk
    file.delete() 
    return redirect("subjects:category_detail", pk=category_pk)
# --------------------------
# Delete Subcategory (Function-Based View)
# --------------------------
@login_required
def delete_subcategory(request, pk):
    subcategory = get_object_or_404(Category, pk=pk)
    parent_category_pk = subcategory.parent.pk if subcategory.parent else subcategory.subject.pk

    if subcategory.files.exists() or subcategory.subcategories.exists():
        return redirect("subjects:category_detail", pk=parent_category_pk)

    subcategory.delete()
    return redirect("subjects:category_detail", pk=parent_category_pk)
