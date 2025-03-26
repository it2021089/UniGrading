from django.shortcuts import render, get_object_or_404, redirect
from django.http import Http404, JsonResponse, HttpResponseRedirect, FileResponse
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
import mimetypes
import logging
from django.conf import settings
import boto3
from botocore.exceptions import ClientError

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
        if self.request.user.role == "professor":
            dashboard_url = reverse_lazy("users:professor_dashboard")
        elif self.request.user.role == "student":
            dashboard_url = reverse_lazy("users:student_dashboard")
        else:
            dashboard_url = reverse_lazy("users:login")

        return [
            ("Dashboard", dashboard_url),
            ("My Subjects", reverse_lazy("subjects:my_subjects")),
            (f"Subject: {self.object.name}", self.request.path),
        ]

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["categories"] = self.object.categories.filter(parent__isnull=True)
        return context

    def post(self, request, *args, **kwargs):
        subject = self.get_object()
        data = request.POST

        if "description" in data:
            new_description = data.get("description", "").strip()
            if new_description:
                subject.description = new_description
                subject.save()
                return JsonResponse({"status": "success", "message": "Description updated!"})

        elif "new_category" in data:
            category_name = data.get("new_category", "").strip()
            if category_name:
                category = Category.objects.create(subject=subject, name=category_name, parent=None)
                return JsonResponse({"status": "success", "message": "Category added!", "category_id": category.id, "category_name": category.name})

        elif "delete_category" in data:
            category_id = data.get("category_id")
            category = get_object_or_404(Category, id=category_id)
            category.delete()
            return JsonResponse({"status": "success", "message": "Category deleted!"})

        return redirect(self.request.path)

# Category Detail View
class CategoryDetailView(LoginRequiredMixin, BreadcrumbMixin, DetailView):
    model = Category
    template_name = "category_detail.html"
    context_object_name = "category"

    def get_breadcrumbs(self):
        category = self.get_object()
        dashboard_url = reverse_lazy("users:login")
        if self.request.user.role == "professor":
            dashboard_url = reverse_lazy("users:professor_dashboard")
        elif self.request.user.role == "student":
            dashboard_url = reverse_lazy("users:student_dashboard")

        return [
            ("Dashboard", dashboard_url),
            ("My Subjects", reverse_lazy("subjects:my_subjects")),
            (f"Subject: {category.subject.name}", reverse_lazy("subjects:subject_detail", args=[category.subject.pk])),
            (f"Category: {category.name}", self.request.path),
        ]

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        category = self.object
        files = category.files.all()

        # Enhance file metadata
        for file in files:
            file.extension = file.name.split('.')[-1].lower()
            file.mimetype, _ = mimetypes.guess_type(file.file.url)
            try:
                file.size_kb = round(file.file.size / 1024, 2)
            except Exception:
                file.size_kb = "-"

        context["breadcrumbs"] = self.get_breadcrumbs()
        context["subcategories"] = category.subcategories.all()
        context["files"] = files
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
                File.objects.create(category=category, name=uploaded_file.name, file=uploaded_file, uploaded_by=request.user)
                messages.success(request, "File uploaded successfully!")
            except Exception as e:
                messages.error(request, f"Upload failed: {str(e)}")

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
    logger.info(f"[DELETE FILE] Request to delete file {file.name} ({file.file.name})")

    category_pk = file.category.pk  
    file.delete()

    logger.info("[DELETE FILE] File deleted successfully.")
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

# --------------------------
# Download file (Function-Based View)
# --------------------------

def download_file(request, file_id):
    file = get_object_or_404(File, id=file_id)

    s3_client = boto3.client(
        's3',
        endpoint_url=settings.AWS_S3_ENDPOINT_URL,
        aws_access_key_id=settings.AWS_ACCESS_KEY_ID,
        aws_secret_access_key=settings.AWS_SECRET_ACCESS_KEY,
    )

    try:
        # Generate presigned URL
        presigned_url = s3_client.generate_presigned_url(
            'get_object',
            Params={'Bucket': settings.AWS_STORAGE_BUCKET_NAME, 'Key': file.file.name},
            ExpiresIn=3600,
        )

        # Replace "minio" with "localhost" for browser use
        browser_url = presigned_url.replace("http://minio:9000", "http://localhost:9000")

        return HttpResponseRedirect(browser_url)

    except Exception as e:
        raise Http404("Could not generate download link.")
    
# --------------------------
# Preview file (Function-Based View)
# --------------------------

@login_required
def preview_file(request, pk):
    file = get_object_or_404(File, pk=pk)

    try:
        # Open file using Django storage backend
        file_handle = file.file.open("rb")
        return FileResponse(file_handle, content_type=file.file.file.content_type)
    except Exception:
        raise Http404("File could not be previewed.")