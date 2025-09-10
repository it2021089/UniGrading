# assignments/models.py
from pathlib import Path, PurePosixPath

from django.db import models
from django.utils import timezone
from django.utils.text import slugify, get_valid_filename


def _seg(value: str, fallback: str) -> str:
    """Unicode-safe slug for a single path segment, with fallback if empty."""
    s = slugify((value or "").strip(), allow_unicode=True).strip("/\\.")
    return s or fallback


def _fname(name: str) -> str:
    """Keep only basename and make it filesystem-safe."""
    base = PurePosixPath((name or "")).name.lstrip("/\\.")
    safe = get_valid_filename(base)
    return safe or "file"


def assignment_upload_path(instance, filename):
    prof = _seg(
        getattr(instance.professor, "get_full_name", lambda: "")() or instance.professor.username,
        fallback=f"user-{getattr(instance.professor, 'pk', 'x')}"
    )
    subj = _seg(instance.subject.name, fallback=f"subject-{getattr(instance.subject, 'pk', 'x')}")
    fname = _fname(filename)
    return "/".join([prof, subj, "assignment-files", fname])


class Assignment(models.Model):
    title = models.CharField(max_length=255)
    description = models.TextField()
    subject = models.ForeignKey("subjects.Subject", related_name="assignments", on_delete=models.CASCADE)
    professor = models.ForeignKey("users.CustomUser", limit_choices_to={"role": "professor"}, on_delete=models.CASCADE)
    due_date = models.DateTimeField()
    file = models.FileField(upload_to=assignment_upload_path, blank=True, null=True, max_length=1024)

    # Autograder config
    autograde_enabled = models.BooleanField(default=True)
    autograde_max_tokens = models.PositiveIntegerField(default=16000)
    autograde_leniency = models.PositiveIntegerField(default=5)
    autograde_weight_runtime = models.FloatField(default=0.7)
    autograde_weight_rubric = models.FloatField(default=0.3)

    # Scheduling flags
    autograde_job_scheduled = models.BooleanField(default=False)
    autograde_done_at = models.DateTimeField(blank=True, null=True)

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


def submission_upload_path(instance, filename):
    a = instance.assignment
    prof = _seg(
        a.subject.professor.get_full_name() or a.subject.professor.username,
        fallback=f"user-{getattr(a.subject.professor, 'pk', 'x')}"
    )
    subj = _seg(a.subject.name, fallback=f"subject-{getattr(a.subject, 'pk', 'x')}")
    a_slug = _seg(a.title, fallback=f"assignment-{getattr(a, 'pk', 'x')}")
    student = _seg(
        instance.student.get_full_name() or instance.student.username,
        fallback=f"user-{getattr(instance.student, 'pk', 'x')}"
    )
    fname = _fname(filename)
    return "/".join([prof, subj, "assignment-submissions", a_slug, student, fname])


class AssignmentSubmission(models.Model):
    assignment = models.ForeignKey(Assignment, related_name="submissions", on_delete=models.CASCADE)
    student = models.ForeignKey("users.CustomUser", related_name="assignment_submissions", on_delete=models.CASCADE)
    file = models.FileField(upload_to=submission_upload_path, max_length=1024)
    submitted_at = models.DateTimeField(default=timezone.now)

    grade_pct = models.FloatField(null=True, blank=True)

    AUTOGRADE_STATUS = [
        ("queued", "Queued"),
        ("running", "Running"),
        ("done", "Done"),
        ("failed", "Failed"),
        ("await_manual", "Await manual review"),
    ]
    autograde_status = models.CharField(max_length=12, choices=AUTOGRADE_STATUS, default="queued")
    autograde_report = models.JSONField(blank=True, null=True)
    ai_feedback = models.TextField(blank=True)
    runner_logs = models.TextField(blank=True)

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
