# Generated by Django 5.1.2 on 2024-11-06 23:11

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('users', '0002_remove_institution_id_institution_institution_id_and_more'),
    ]

    operations = [
        migrations.AlterField(
            model_name='profile',
            name='role',
            field=models.CharField(choices=[('student', 'Student'), ('professor', 'Professor'), ('admin', 'Admin')], max_length=10),
        ),
    ]
