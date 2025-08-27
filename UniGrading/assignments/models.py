from django.db import models
from django.utils.text import slugify
from subjects.models import Subject
from pathlib import Path
from users.models import CustomUser

def assignment_upload_path(instance, filename):
    """Store under: <prof>/<subject>/assignment-files/<filename>, slugs for safety."""
    prof = (instance.professor.get_full_name() or instance.professor.username or "prof").strip()
    subj = (instance.subject.name or "subject").strip()
    return f"{slugify(prof)}/{slugify(subj)}/assignment-files/{filename}"

class Assignment(models.Model):
    title = models.CharField(max_length=255)
    description = models.TextField()
    subject = models.ForeignKey(Subject, related_name="assignments", on_delete=models.CASCADE)
    professor = models.ForeignKey(
        CustomUser, limit_choices_to={'role': 'professor'}, on_delete=models.CASCADE
    )
    due_date = models.DateTimeField()
    @property
    def file_basename(self) -> str:
        return Path(self.file.name).name if self.file else ""
    # Let Django use whatever DEFAULT_FILE_STORAGE is configured to (local or S3):
    file = models.FileField(upload_to=assignment_upload_path, blank=True, null=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.title} - {self.subject.name}"

    def delete(self, *args, **kwargs):
        # Clean up the object in storage on deletion.
        if self.file and self.file.storage.exists(self.file.name):
            self.file.storage.delete(self.file.name)
        super().delete(*args, **kwargs)
