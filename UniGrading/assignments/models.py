# assignments/models.py
from pathlib import Path
from django.db import models
from django.utils.text import slugify
from django.utils import timezone
from users.models import CustomUser
from subjects.models import Subject


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
    file = models.FileField(upload_to=assignment_upload_path, blank=True, null=True)

    # --- Auto-grading config (simple hints; you can ignore in UI if you want) ---
    autograde_enabled = models.BooleanField(default=True)
    autograde_max_tokens = models.PositiveIntegerField(default=16000)
    autograde_leniency = models.PositiveIntegerField(default=5)        # 1=strict .. 5=very lenient
    autograde_weight_runtime = models.FloatField(default=0.7)
    autograde_weight_rubric = models.FloatField(default=0.3)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    @property
    def file_basename(self) -> str:
        return Path(self.file.name).name if self.file else ""

    def __str__(self):
        return f"{self.title} - {self.subject.name}"

    def delete(self, *args, **kwargs):
        if self.file and self.file.storage.exists(self.file.name):
            self.file.storage.delete(self.file.name)
        super().delete(*args, **kwargs)


# ----------------------------
# Student submissions + AI grade
# ----------------------------
def submission_upload_path(instance, filename):
    """
    Store submissions under:
      <prof>/<subject>/assignment-submissions/<assignment-slug>/<student-slug>/<filename>
    """
    a = instance.assignment
    prof = slugify(a.subject.professor.get_full_name() or a.subject.professor.username or "prof")
    subj = slugify(a.subject.name or "subject")
    a_slug = slugify(a.title or f"assignment-{a.pk}")
    student = slugify(instance.student.get_full_name() or instance.student.username or f"user-{instance.student_id}")
    return f"{prof}/{subj}/assignment-submissions/{a_slug}/{student}/{filename}"


class AssignmentSubmission(models.Model):
    assignment = models.ForeignKey(Assignment, related_name="submissions", on_delete=models.CASCADE)
    student = models.ForeignKey(CustomUser, related_name="assignment_submissions", on_delete=models.CASCADE)
    file = models.FileField(upload_to=submission_upload_path)
    submitted_at = models.DateTimeField(default=timezone.now)

    # numeric grade (0..100)
    grade_pct = models.FloatField(null=True, blank=True)

    # --- AI grading state ---
    AUTOGRADE_STATUS = [
        ("queued",  "Queued"),
        ("running", "Running"),
        ("done",    "Done"),
        ("failed",  "Failed"),
    ]
    autograde_status = models.CharField(max_length=12, choices=AUTOGRADE_STATUS, default="queued")
    autograde_report = models.JSONField(blank=True, null=True)  # structured result
    ai_feedback = models.TextField(blank=True)                  # narrative feedback
    runner_logs = models.TextField(blank=True)                  # build/run notes (truncated)
    graded_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        unique_together = (("assignment", "student"),)
        ordering = ["-submitted_at"]

    def __str__(self):
        return f"Submission of {self.student} for {self.assignment}"

    def delete(self, *args, **kwargs):
        storage = self.file.storage
        if self.file and storage and storage.exists(self.file.name):
            storage.delete(self.file.name)
        super().delete(*args, **kwargs)
