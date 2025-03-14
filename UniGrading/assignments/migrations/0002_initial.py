# Generated by Django 5.1.7 on 2025-03-14 18:10

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    initial = True

    dependencies = [
        ('assignments', '0001_initial'),
        ('subjects', '0001_initial'),
    ]

    operations = [
        migrations.AddField(
            model_name='assignment',
            name='subject',
            field=models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, to='subjects.subject'),
        ),
    ]
