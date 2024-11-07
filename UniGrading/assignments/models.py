from django.db import models
from subjects.models import Subject

class Assignment(models.Model):
    title = models.CharField(max_length=100)
    description = models.TextField()
    subject = models.ForeignKey(Subject, on_delete=models.CASCADE)
    due_date = models.DateField()
    is_test = models.BooleanField(default=False)  # Distinguishes between assignment and test

    def __str__(self):
        return self.title