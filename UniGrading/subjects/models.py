from django.db import models
from users.models import CustomUser

class Subject(models.Model):
    name = models.CharField(max_length=100)
    professor = models.ForeignKey(CustomUser, limit_choices_to={'role': 'professor'}, on_delete=models.CASCADE, editable=False)
    description = models.TextField(blank=True)

    def __str__(self):
        return self.name