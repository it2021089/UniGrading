from django.db import models
from users.models import CustomUser
from django.conf import settings
from storages.backends.s3boto3 import S3Boto3Storage

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
def get_file_storage():
    """Return the correct storage backend."""
    return S3Boto3Storage() if settings.DEFAULT_FILE_STORAGE == 'storages.backends.s3boto3.S3Boto3Storage' else None
class File(models.Model):
    category = models.ForeignKey('Category', related_name='files', on_delete=models.CASCADE)
    name = models.CharField(max_length=100)
    file = models.FileField(upload_to='files/', storage=get_file_storage())  

    def __str__(self):
        return self.name
