from django.db import models
from assignments.models import Assignment
from users.models import CustomUser

class Submission(models.Model):
    student = models.ForeignKey(CustomUser, limit_choices_to={'role': 'student'}, on_delete=models.CASCADE)
    assignment = models.ForeignKey(Assignment, on_delete=models.CASCADE)
    submitted_file = models.FileField(upload_to='submissions/', blank=True, null=True)
    submitted_text = models.TextField(blank=True, null=True)
    submission_date = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"Submission by {self.student} for {self.assignment}"

class Grade(models.Model):
    submission = models.OneToOneField(Submission, on_delete=models.CASCADE)
    grade_value = models.FloatField()
    feedback = models.TextField(blank=True)

    def __str__(self):
        return f"Grade for {self.submission}: {self.grade_value}"
