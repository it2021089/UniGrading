from django.db import models
from subjects.models import Subject 
from users.models import CustomUser  
from django.conf import settings
from storages.backends.s3boto3 import S3Boto3Storage


def get_file_storage():
    """Return the correct storage backend."""
    return S3Boto3Storage() if settings.DEFAULT_FILE_STORAGE == 'storages.backends.s3boto3.S3Boto3Storage' else None


class Assignment(models.Model):
    title = models.CharField(max_length=255)
    description = models.TextField()
    subject = models.ForeignKey(Subject, related_name="assignments", on_delete=models.CASCADE)
    professor = models.ForeignKey(
        CustomUser, limit_choices_to={'role': 'professor'}, on_delete=models.CASCADE
    )
    due_date = models.DateTimeField()
    file = models.FileField(upload_to='assignments/', storage=get_file_storage(), blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.title} - {self.subject.name}"
    
    