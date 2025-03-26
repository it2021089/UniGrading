from django.db import models
from users.models import CustomUser
from django.conf import settings
from storages.backends.s3boto3 import S3Boto3Storage
from django.utils.text import slugify


def get_file_storage():
    return S3Boto3Storage() if settings.DEFAULT_FILE_STORAGE == 'storages.backends.s3boto3.S3Boto3Storage' else None


def subject_file_upload_path(instance, filename):
    user = instance.uploaded_by
    professor_name = slugify(instance.category.subject.professor.get_full_name() or instance.category.subject.professor.username)
    subject_name = slugify(instance.category.subject.name)
    category_name = slugify(instance.category.name)

    if user.role == "student":
        student_name = slugify(user.get_full_name() or user.username)
        return f"{professor_name}/{subject_name}/{category_name}/{student_name}/{filename}"
    else:
        if category_name.lower() in ["assignments", "tests"]:
            return f"{professor_name}/{subject_name}/{category_name} Files/{filename}"
        return f"{professor_name}/{subject_name}/{category_name}/{filename}"


class Subject(models.Model):
    name = models.CharField(max_length=100)
    professor = models.ForeignKey(CustomUser, limit_choices_to={'role': 'professor'}, on_delete=models.CASCADE, editable=False)
    description = models.TextField(blank=True)

    def __str__(self):
        return self.name

    class Meta:
        ordering = ['name']


class Category(models.Model):
    subject = models.ForeignKey(Subject, related_name='categories', on_delete=models.CASCADE)
    name = models.CharField(max_length=255)
    parent = models.ForeignKey('self', null=True, blank=True, related_name='subcategories', on_delete=models.CASCADE)

    class Meta:
        unique_together = (('subject', 'name', 'parent'),)

    def __str__(self):
        return self.name


class File(models.Model):
    category = models.ForeignKey('Category', related_name='files', on_delete=models.CASCADE)
    name = models.CharField(max_length=100)
    file = models.FileField(upload_to=subject_file_upload_path, storage=get_file_storage())
    uploaded_by = models.ForeignKey(CustomUser, null=True, blank=True, on_delete=models.SET_NULL)

    def __str__(self):
        return self.name

    def delete(self, *args, **kwargs):
        storage = self.file.storage
        if storage.exists(self.file.name):
            storage.delete(self.file.name)
        super().delete(*args, **kwargs)
