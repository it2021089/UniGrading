# Generated by Django 5.0.7 on 2025-03-26 09:45

import assignments.models
import storages.backends.s3
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('assignments', '0003_remove_assignment_is_test_assignment_created_at_and_more'),
    ]

    operations = [
        migrations.AlterField(
            model_name='assignment',
            name='file',
            field=models.FileField(blank=True, null=True, storage=storages.backends.s3.S3Storage(), upload_to=assignments.models.assignment_upload_path),
        ),
    ]
