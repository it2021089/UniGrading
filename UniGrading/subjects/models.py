# subjects/models.py
from django.db import models
from django.utils.text import slugify
from django.conf import settings
from users.models import CustomUser


def subject_file_upload_path(instance, filename):
    """
    S3/MinIO object key layout:
      <prof>/<subject>/<category>/[<student>/]<filename>
      - For Assignments/Tests uploaded by the professor: "<category> Files" bucket folder.
      - All directory parts are slugified for safety; filename kept as-is.
    """
    user = instance.uploaded_by
    professor_name = slugify(instance.category.subject.professor.get_full_name()
                             or instance.category.subject.professor.username)
    subject_name = slugify(instance.category.subject.name)
    category_name = slugify(instance.category.name)

    if getattr(user, "role", None) == "student":
        student_name = slugify(user.get_full_name() or user.username)
        return f"{professor_name}/{subject_name}/{category_name}/{student_name}/{filename}"
    else:
        if category_name.lower() in ["assignments", "tests"]:
            return f"{professor_name}/{subject_name}/{category_name} Files/{filename}"
        return f"{professor_name}/{subject_name}/{category_name}/{filename}"


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
    # IMPORTANT: no explicit storage=... — uses STORAGES["default"] (S3Boto3/MinIO)
    file = models.FileField(upload_to=subject_file_upload_path)
    uploaded_by = models.ForeignKey(CustomUser, null=True, blank=True, on_delete=models.SET_NULL)
    uploaded_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.name

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
