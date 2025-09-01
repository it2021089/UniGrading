import os
from celery import Celery # type: ignore

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "UniGrading.settings")

app = Celery("UniGrading")
app.config_from_object("django.conf:settings", namespace="CELERY")
app.autodiscover_tasks()

app.conf.beat_schedule = {
    "enqueue-due-autogrades-every-minute": {
        "task": "assignments.tasks.enqueue_due_autogrades",
        "schedule": 60.0,
    },
}