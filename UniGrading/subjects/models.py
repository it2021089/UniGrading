# subjects/models.py
from pathlib import PurePosixPath

from django.conf import settings
from django.db import models
from django.utils.text import slugify, get_valid_filename

from users.models import CustomUser


def _seg(value: str, fallback: str) -> str:
    """Unicode-safe slug for a single path segment, with fallback if empty."""
    s = slugify((value or "").strip(), allow_unicode=True).strip("/\\.")
    return s or fallback


def _fname(name: str) -> str:
    """Keep only basename, drop leading slashes, and make it filesystem-safe."""
    base = PurePosixPath((name or "")).name.lstrip("/\\.")
    safe = get_valid_filename(base)
    return safe or "file"


def subject_file_upload_path(instance, filename):
    """
    Object key layout:
      <prof>/<subject>/<category>/[<student>/]<filename>
    Professor uploads to Assignments/Tests go to '<category>-files'.
    """
    user = instance.uploaded_by
    prof = _seg(
        instance.category.subject.professor.get_full_name() or instance.category.subject.professor.username,
        fallback=f"user-{getattr(instance.category.subject.professor, 'pk', 'x')}"
    )
    subj = _seg(instance.category.subject.name, fallback=f"subject-{getattr(instance.category.subject, 'pk', 'x')}")
    cat_slug = _seg(instance.category.name, fallback="category")
    fname = _fname(filename)

    parts = [prof, subj]

    if getattr(user, "role", None) == "student":
        student = _seg(user.get_full_name() or user.username, fallback=f"user-{getattr(user, 'pk', 'x')}")
        parts += [cat_slug, student, fname]
    else:
        folder = f"{cat_slug}-files" if cat_slug in {"assignments", "tests"} else cat_slug
        parts += [folder, fname]

    return "/".join(parts)


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
    file = models.FileField(upload_to=subject_file_upload_path, max_length=1024)
    uploaded_by = models.ForeignKey(CustomUser, null=True, blank=True, on_delete=models.SET_NULL)
    uploaded_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.name

    # IMPORTANT: no save() override that rewrites self.file.name

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
