from django.shortcuts import render, get_object_or_404, redirect
from django.http import Http404, JsonResponse, HttpResponseRedirect, FileResponse, HttpResponse
from django.contrib.auth.decorators import login_required
from django.views.decorators.csrf import csrf_exempt
from django.views.generic import DetailView, ListView, CreateView
from django.contrib.auth.mixins import LoginRequiredMixin
from django.views.decorators.http import require_POST
from django.urls import reverse_lazy
from .models import Subject, Category, File
from .forms import SubjectForm
from UniGrading.mixin import BreadcrumbMixin
from django.db import transaction
from django.contrib import messages
import mimetypes
import logging
from botocore.exceptions import ClientError
from django.conf import settings
from django.views.decorators.http import require_GET
from django.views.decorators.clickjacking import xframe_options_exempt

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
        if getattr(self.request.user, "role", None) == "professor":
            dashboard_url = reverse_lazy("users:professor_dashboard")
        elif getattr(self.request.user, "role", None) == "student":
            dashboard_url = reverse_lazy("users:student_dashboard")

        return [
            ("Dashboard", dashboard_url),
            ("My Subjects", reverse_lazy("subjects:my_subjects")),
        ]

    def get_queryset(self):
        """
        Professors: show their own subjects first (adjust if you want strict filtering)
        Students / others: default ordering
        """
        qs = Subject.objects.select_related("professor").order_by("name")
        if getattr(self.request.user, "role", None) == "professor":
            return qs.filter(professor=self.request.user)
        return qs

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
        if getattr(self.request.user, "role", None) == "professor":
            dashboard_url = reverse_lazy("users:professor_dashboard")
        elif getattr(self.request.user, "role", None) == "student":
            dashboard_url = reverse_lazy("users:student_dashboard")

        context['breadcrumbs'] = [
            ("Dashboard", dashboard_url),
            ("My Subjects", reverse_lazy("subjects:my_subjects")),
            ("Create Subject", "")
        ]
        return context

    def form_valid(self, form):
        """
        Prevent duplicate subject per professor (case-insensitive) on create.
        If duplicate, render the same page with an error message so the modal can show.
        """
        subject = form.save(commit=False)
        subject.professor = self.request.user
        name = (subject.name or "").strip()

        # Duplicate check scoped to professor
        if Subject.objects.filter(professor=self.request.user, name__iexact=name).exists():
            from django.contrib import messages
            messages.error(self.request, "A subject with this name already exists.")
            # Re-render the form page (no redirect), so template JS can pop the modal
            form.add_error("name", "Duplicate name for your account.")
            return self.render_to_response(self.get_context_data(form=form))

        with transaction.atomic():
            subject.name = name
            subject.save()

            # Default categories
            default_categories = ["Courses", "Assignments", "Tests", "Other"]
            for category_name in default_categories:
                Category.objects.get_or_create(subject=subject, name=category_name, parent=None)

            # Additional categories from POST
            additional_categories = self.request.POST.getlist('categories')
            for category_name in additional_categories:
                category_name = (category_name or "").strip()
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
        if getattr(self.request.user, "role", None) == "professor":
            dashboard_url = reverse_lazy("users:professor_dashboard")
        elif getattr(self.request.user, "role", None) == "student":
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
        context["protected_categories"] = ["Courses", "Assignments", "Tests", "Other"]
        context["breadcrumbs"] = self.get_breadcrumbs()
        return context

    def post(self, request, *args, **kwargs):
        subject = self.get_object()
        data = request.POST
        protected_categories = ["Courses", "Assignments", "Tests", "Other"]
        
          # === Update subject name (title) ===
        if "update_subject_name" in data:
            new_name = (data.get("subject_name") or "").strip()
            if not new_name:
                return JsonResponse({"status": "error", "message": "Subject name cannot be empty."}, status=400)

            if Subject.objects.filter(
                professor=subject.professor,
                name__iexact=new_name
            ).exclude(pk=subject.pk).exists():
                return JsonResponse(
                    {"status": "error", "message": "A subject with this name already exists."},
                    status=409,
                )

            subject.name = new_name
            subject.save(update_fields=["name"])
            return JsonResponse({"status": "success", "message": "Subject name updated!"})

        # === Update subject description ===
        if "description" in data:
            new_description = data.get("description", "").strip()
            if new_description:
                subject.description = new_description
                subject.save()
                return JsonResponse({"status": "success", "message": "Description updated!"})

        # === Add new category ===
        elif "new_category" in data:
            category_name = data.get("new_category", "").strip()
            if category_name:
                if Category.objects.filter(subject=subject, name=category_name, parent=None).exists():
                    return JsonResponse({"status": "error", "message": "A category with this name already exists."})

                category = Category.objects.create(subject=subject, name=category_name, parent=None)
                return JsonResponse({
                    "status": "success",
                    "message": "Category added!",
                    "category_id": category.id,
                    "category_name": category.name
                })

        # === Delete a category ===
        elif "delete_category" in data:
            category_id = data.get("category_id")
            category = get_object_or_404(Category, id=category_id)

            if category.parent is None and category.name in protected_categories:
                return JsonResponse({"status": "error", "message": "This category cannot be deleted."})

            category.delete()
            return JsonResponse({"status": "success", "message": "Category deleted!"})

        # === Update category name ===
        elif "update_category" in data:
            category_id = data.get("category_id")
            new_name = data.get("category_name", "").strip()

            if not new_name:
                return JsonResponse({"status": "error", "message": "Name cannot be empty."})

            try:
                category = Category.objects.get(id=category_id, subject=subject)

                if category.parent is None and category.name in protected_categories:
                    return JsonResponse({"status": "error", "message": "This category cannot be renamed."})

                if Category.objects.filter(subject=subject, name=new_name, parent=None).exclude(id=category.id).exists():
                    return JsonResponse({"status": "error", "message": "A category with this name already exists."})

                category.name = new_name
                category.save()
                return JsonResponse({"status": "success", "message": "Category name updated!"})
            except Category.DoesNotExist:
                return JsonResponse({"status": "error", "message": "Category not found."})

        return redirect(self.request.path)

# --------------------------
# Rename Subject (Function-Based View, AJAX)
# --------------------------
@login_required
@require_POST
def rename_subject(request, pk):
    """
    Rename a subject with per-professor duplicate prevention (case-insensitive).
    Returns JSON. 409 on duplicate.
    """
    subject = get_object_or_404(Subject, pk=pk)

    # Only the owning professor can rename (adjust if your auth rules differ)
    if subject.professor != request.user:
        return JsonResponse({"ok": False, "message": "Not allowed."}, status=403)

    new_name = (request.POST.get("name") or "").strip()
    if not new_name:
        return JsonResponse({"ok": False, "message": "Name cannot be empty."}, status=400)

    # If unchanged (case-insensitive), accept
    if subject.name.strip().casefold() == new_name.casefold():
        return JsonResponse({"ok": True, "id": subject.id, "name": subject.name})

    # Duplicate check per professor (exclude self)
    if Subject.objects.filter(
        professor=request.user,
        name__iexact=new_name
    ).exclude(pk=subject.id).exists():
        return JsonResponse(
            {"ok": False, "reason": "duplicate", "message": "A subject with this name already exists."},
            status=409
        )

    subject.name = new_name
    subject.save(update_fields=["name"])
    return JsonResponse({"ok": True, "id": subject.id, "name": subject.name})

# --------------------------
# Category Detail View
# --------------------------

class CategoryDetailView(LoginRequiredMixin, BreadcrumbMixin, DetailView):
    model = Category
    template_name = "category_detail.html"
    context_object_name = "category"

    def get_breadcrumbs(self):
        category = self.get_object()

        # Get correct dashboard based on role
        if getattr(self.request.user, "role", None) == "professor":
            dashboard_url = reverse_lazy("users:professor_dashboard")
        elif getattr(self.request.user, "role", None) == "student":
            dashboard_url = reverse_lazy("users:student_dashboard")
        else:
            dashboard_url = reverse_lazy("users:login")

        # Traverse up through parent categories to build path
        breadcrumb_categories = []
        current = category
        while current is not None:
            breadcrumb_categories.insert(0, current)
            current = current.parent

        # Build final breadcrumb list
        breadcrumbs = [
            ("Dashboard", dashboard_url),
            ("My Subjects", reverse_lazy("subjects:my_subjects")),
            (f"Subject: {category.subject.name}", reverse_lazy("subjects:subject_detail", args=[category.subject.pk])),
        ]

        for c in breadcrumb_categories:
            if c == category:
                breadcrumbs.append((f"Category: {c.name}", self.request.path))
            else:
                breadcrumbs.append((f"Category: {c.name}", reverse_lazy("subjects:category_detail", args=[c.pk])))

        return breadcrumbs

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        category = self.object

        # Subcategories
        context["subcategories"] = category.subcategories.all()

        # Files with metadata
        files = []
        for f in category.files.all():
            try:
                size_kb = round(f.file.size / 1024, 2)
                mimetype, _ = mimetypes.guess_type(f.file.url)
                is_missing = False
            except (FileNotFoundError, ClientError) as e:
                logger.warning(f"Missing file: {f.name} — {e}")
                size_kb = "-"
                mimetype = "unknown/unknown"
                is_missing = True

            f.extension = f.name.split('.')[-1].lower()
            f.size_kb = size_kb
            f.mimetype = mimetype
            f.is_missing = is_missing
            files.append(f)

        context["files"] = files
        context["breadcrumbs"] = self.get_breadcrumbs()
        return context

    def post(self, request, *args, **kwargs):
        category = self.get_object()
        data = request.POST

        # Helpers
        def jerr(msg, code=400):
            return JsonResponse({"status": "error", "message": msg}, status=code)

        def jok(payload=None):
            base = {"status": "success"}
            if payload:
                base.update(payload)
            return JsonResponse(base)

        # Create subfolder (AJAX)
        if "new_subcategory" in data:
            name = (data.get("new_subcategory") or "").strip()
            if not name:
                return jerr("Folder name cannot be empty.")
            # case-insensitive uniqueness under *this* parent
            if Category.objects.filter(
                subject=category.subject, parent=category, name__iexact=name
            ).exists():
                return jerr("A folder with this name already exists.", code=409)

            sub = Category.objects.create(subject=category.subject, parent=category, name=name)
            return jok({"id": sub.id, "name": sub.name})

        # Rename subfolder (AJAX)
        if "update_subcategory" in data:
            sub_id = data.get("subcategory_id")
            new_name = (data.get("subcategory_name") or "").strip()
            if not new_name:
                return jerr("Folder name cannot be empty.")

            try:
                sub = Category.objects.get(id=sub_id, parent=category)
            except Category.DoesNotExist:
                return jerr("Folder not found.", code=404)

            # case-insensitive uniqueness under *this* parent, excluding self
            if Category.objects.filter(
                subject=category.subject, parent=category, name__iexact=new_name
            ).exclude(id=sub.id).exists():
                return jerr("A folder with this name already exists.", code=409)

            sub.name = new_name
            sub.save(update_fields=["name"])
            return jok({"id": sub.id, "name": sub.name})

        # Delete subfolder (AJAX)
        if "delete_subcategory" in data:
            sub_id = data.get("subcategory_id")
            try:
                sub = Category.objects.get(id=sub_id, parent=category)
            except Category.DoesNotExist:
                return jerr("Folder not found.", code=404)

            if sub.files.exists() or sub.subcategories.exists():
                return jerr("Cannot delete a non-empty folder.", code=409)

            sub.delete()
            return jok()

                # Upload file (supports AJAX FormData and normal form submission)
        if "new_file" in request.POST or (
            request.headers.get("X-Requested-With") == "XMLHttpRequest" and "file" in request.FILES
        ):
            if "file" not in request.FILES:
                if request.headers.get("X-Requested-With") == "XMLHttpRequest":
                    return JsonResponse({"status": "error", "message": "No file provided."}, status=400)
                messages.error(request, "No file provided.")
                return redirect(self.request.path)

            uploaded_file = request.FILES["file"]
            try:
                File.objects.create(
                    category=category,
                    name=uploaded_file.name,
                    file=uploaded_file,
                    uploaded_by=request.user,
                )
                if request.headers.get("X-Requested-With") == "XMLHttpRequest":
                    return JsonResponse({"status": "success"})
                messages.success(request, "File uploaded successfully!")
            except Exception as e:
                if request.headers.get("X-Requested-With") == "XMLHttpRequest":
                    return JsonResponse({"status": "error", "message": f"Upload failed: {str(e)}"}, status=500)
                messages.error(request, f"Upload failed: {str(e)}")
            return redirect(self.request.path)
        # Delete file (non-AJAX form post)
        if "delete_file" in data:
            file_id = data.get("file_id")
            file = get_object_or_404(File, id=file_id, category=category)
            try:
                file.delete()
                messages.success(request, "File deleted successfully!")
            except Exception as e:
                messages.error(request, f"Failed to delete file: {str(e)}")
            return redirect(self.request.path)

        # Fallback
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
    logger.info(f"Attempting to delete file {file.name} by user {request.user.username}")

    if request.user != file.uploaded_by and not request.user.is_superuser:
        logger.warning("Permission denied for deleting file.")
        messages.error(request, "You do not have permission to delete this file.")
        return redirect('subjects:category_detail', pk=file.category.pk)

    try:
        file.delete()
        messages.success(request, "File deleted successfully.")
        logger.info("File deleted successfully.")
    except Exception as e:
        logger.error(f"Error deleting file: {e}")
        messages.error(request, "An error occurred while deleting the file.")
    return redirect('subjects:category_detail', pk=file.category.pk)

# --------------------------
# Delete Subcategory (Function-Based View)
# --------------------------
@login_required
def delete_subcategory(request, pk):
    subcategory = get_object_or_404(Category, pk=pk)
    parent_category_pk = subcategory.parent.pk if subcategory.parent else subcategory.subject.pk

    if subcategory.files.exists() or subcategory.subcategories.exists():
        messages.error(request, "Cannot delete a non-empty folder.")
        return redirect("subjects:category_detail", pk=parent_category_pk)

    try:
        subcategory.delete()
        messages.success(request, "Folder deleted successfully.")
    except Exception as e:
        messages.error(request, f"An error occurred while deleting the folder: {e}")
    return redirect("subjects:category_detail", pk=parent_category_pk)

# --------------------------
# Download File (Function-Based View)
# --------------------------
@require_GET
def download_file(request, file_id):
    file_obj = get_object_or_404(File, pk=file_id)

    try:
        content_type, _ = mimetypes.guess_type(file_obj.file.name)
        content_type = content_type or 'application/octet-stream'
        file_data = file_obj.file.open()

        response = HttpResponse(file_data, content_type=content_type)
        response['Content-Disposition'] = f'attachment; filename="{file_obj.name}"'
        return response

    except (FileNotFoundError, ClientError) as e:
        logger.warning(f"Download failed — missing file: {file_obj.name} | {e}")
        if request.headers.get("X-Requested-With") == "XMLHttpRequest":
            return JsonResponse({"status": "error", "message": "File not found"}, status=404)
        else:
            # fallback if JS isn't used
            return redirect('subjects:category_detail', pk=file_obj.category.pk)

# --------------------------
# Preview File 
# --------------------------
@xframe_options_exempt
@login_required
def preview_file(request, pk):
    file = get_object_or_404(File, pk=pk)

    try:
        file_handle = file.file.open("rb")
        content_type = mimetypes.guess_type(file.name)[0] or "application/octet-stream"
        response = FileResponse(file_handle, content_type=content_type)
        return response
    except Exception:
        raise Http404("File could not be previewed.")
