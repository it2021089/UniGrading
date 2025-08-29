try:
    from .celery_app import app as celery_app  
except Exception:
    celery_app = None