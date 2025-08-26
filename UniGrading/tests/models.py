from django.conf import settings
from django.db import models
from subjects.models import Subject


class Test(models.Model):
    subject = models.ForeignKey(Subject, on_delete=models.CASCADE, related_name="tests")
    professor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="tests")
    name = models.CharField(max_length=255)
    duration_minutes = models.PositiveIntegerField(default=30)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = (("subject", "professor", "name"),)
        ordering = ["-created_at"]

    def __str__(self):
        return self.name


class Question(models.Model):
    test = models.ForeignKey(Test, on_delete=models.CASCADE, related_name="questions")
    text = models.TextField()

    def __str__(self):
        return f"Q: {self.text[:60]}"


class Choice(models.Model):
    question = models.ForeignKey(Question, on_delete=models.CASCADE, related_name="choices")
    text = models.CharField(max_length=500)
    is_correct = models.BooleanField(default=False)

    def __str__(self):
        return f"{'* ' if self.is_correct else ''}{self.text[:60]}"


class TestAttempt(models.Model):
    test = models.ForeignKey('tests.Test', on_delete=models.CASCADE, related_name='attempts')
    student = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='test_attempts')
    started_at = models.DateTimeField(auto_now_add=True)
    submitted_at = models.DateTimeField(auto_now_add=True)
    score = models.IntegerField(default=0)
    max_score = models.IntegerField(default=0)
    duration_seconds = models.IntegerField(default=0)

    class Meta:
        ordering = ['-submitted_at']

    def __str__(self):
        return f"{self.student} – {self.test} – {self.score}/{self.max_score}"

class AttemptAnswer(models.Model):
    attempt = models.ForeignKey(TestAttempt, on_delete=models.CASCADE, related_name='answers')
    question = models.ForeignKey('tests.Question', on_delete=models.CASCADE)
    choice = models.ForeignKey('tests.Choice', on_delete=models.CASCADE)
    is_correct = models.BooleanField(default=False)

    class Meta:
        unique_together = (('attempt', 'question'),)
