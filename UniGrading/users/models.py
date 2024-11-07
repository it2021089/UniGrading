from django.contrib.auth.models import User
from django.db import models

class Profile(models.Model):
    ROLE_CHOICES = [
        ('student', 'Student'),
        ('professor', 'Professor')
    ]
    id = models.AutoField(primary_key=True)
    user = models.OneToOneField(User, on_delete=models.CASCADE)
    username = models.CharField(max_length=150, default='')
    email = models.EmailField(default='')
    password = models.CharField(max_length=128, default='')
    role = models.CharField(max_length=10, choices=ROLE_CHOICES)
    institution = models.ForeignKey('Institution', on_delete=models.SET_NULL, null=True, blank=True)

    def __str__(self):
        return f"{self.user.username} ({self.role})"
    
class Institution(models.Model):
    institution_id = models.AutoField(primary_key=True, default=1)
    name = models.CharField(max_length=100, unique=True)

    def __str__(self):
        return self.name