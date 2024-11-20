from django.contrib.auth.models import AbstractUser
from django.db import models

class CustomUser(AbstractUser):
    ROLE_CHOICES = [
        ('student', 'Student'),
        ('professor', 'Professor')
    ]
    role = models.CharField(max_length=10, choices=ROLE_CHOICES)
    institution = models.ForeignKey('Institution', on_delete=models.SET_NULL, null=True, blank=True)

    def __str__(self):
        return f"{self.first_name} {self.last_name} ({self.role})"

class Institution(models.Model):
    institution_id = models.AutoField(primary_key=True)
    name = models.CharField(max_length=100, unique=True)

    def __str__(self):
        return self.name