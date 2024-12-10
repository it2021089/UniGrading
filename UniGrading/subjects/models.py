from django.db import models
from users.models import CustomUser

class Subject(models.Model):
    name = models.CharField(max_length=100)
    professor = models.ForeignKey(CustomUser, limit_choices_to={'role': 'professor'}, on_delete=models.CASCADE, editable=False)
    description = models.TextField(blank=True)

    def __str__(self):
        return self.name

class Category(models.Model):
    subject = models.ForeignKey(Subject, related_name='categories', on_delete=models.CASCADE)
    name = models.CharField(max_length=100)
    parent = models.ForeignKey('self', related_name='subcategories', on_delete=models.CASCADE, blank=True, null=True)

    def __str__(self):
        return self.name

class File(models.Model):
    category = models.ForeignKey(Category, related_name='files', on_delete=models.CASCADE)
    name = models.CharField(max_length=100)
    file = models.FileField(upload_to='files/')

    def __str__(self):
        return self.name