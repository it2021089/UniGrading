# subjects/models.py
from pathlib import PurePosixPath

from django.conf import settings
from django.db import models
from django.utils.text import slugify, get_valid_filename

from users.models import CustomUser


def _clean_seg(s: str) -> str:
    """Slugify path segments; strip slashes and dots."""
    return slugify((s or "").strip("/\\."))


def _clean_filename(name: str) -> str:
    """Keep only basename, drop leading slashes, and make it filesystem-safe."""
    base = PurePosixPath((name or "")).name.lstrip("/\\.")
    safe = get_valid_filename(base)
    return safe or "file"


def subject_file_upload_path(instance, filename):
    """
    S3/MinIO object key layout (sanitized):
      <prof>/<subject>/<category>/[<student>/]<filename>
    - All segments are slugified or validated.
    - No leading slash. No double slashes. No client-supplied subpaths in filename.
    - For professor uploads to Assignments/Tests, use '<category>-files' folder.
    """
    user = instance.uploaded_by
    professor_name = _clean_seg(
        instance.category.subject.professor.get_full_name()
        or instance.category.subject.professor.username
    )
    subject_name = _clean_seg(instance.category.subject.name)
    category_name = _clean_seg(instance.category.name)
    fname = _clean_filename(filename)

    parts = [professor_name, subject_name]

    if getattr(user, "role", None) == "student":
        student_name = _clean_seg(user.get_full_name() or user.username)
        parts += [category_name, student_name, fname]
    else:
        if category_name in {"assignments", "tests"}:
            parts += [f"{category_name}-files", fname]
        else:
            parts += [category_name, fname]

    return "/".join(p for p in parts if p)


class Subject(models.Model):
    name = models.CharField(max_length=100)
    professor = models.ForeignKey(
        CustomUser,
        limit_choices_to={"role": "professor"},
        on_delete=models.CASCADE,
        editable=False,
    )
    description = models.TextField(blank=True)

    def __str__(self):
        return self.name

    class Meta:
        ordering = ["name"]


class Category(models.Model):
    subject = models.ForeignKey(Subject, related_name="categories", on_delete=models.CASCADE)
    name = models.CharField(max_length=255)
    parent = models.ForeignKey(
        "self", null=True, blank=True, related_name="subcategories", on_delete=models.CASCADE
    )

    class Meta:
        unique_together = (("subject", "name", "parent"),)

    def __str__(self):
        return self.name


class File(models.Model):
    category = models.ForeignKey("Category", related_name="files", on_delete=models.CASCADE)
    name = models.CharField(max_length=100)
    # Uses default storage (e.g., S3Boto3/MinIO) and sanitized upload_to
    file = models.FileField(upload_to=subject_file_upload_path)
    uploaded_by = models.ForeignKey(CustomUser, null=True, blank=True, on_delete=models.SET_NULL)
    uploaded_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.name

    def save(self, *args, **kwargs):
        if self.file and self.file.name:
            self.file.name = _clean_filename(self.file.name)
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        storage = self.file.storage
        if self.file and storage.exists(self.file.name):
            storage.delete(self.file.name)
        super().delete(*args, **kwargs)


class Enrollment(models.Model):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="enrollments")
    subject = models.ForeignKey(Subject, on_delete=models.CASCADE, related_name="enrollments")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ("user", "subject")
