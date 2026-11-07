"""Project package. Importing ``celery_app`` here makes ``@shared_task`` use this project's Celery app.
"""
from .celery import app as celery_app

__all__ = ("celery_app",)
