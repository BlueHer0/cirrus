"""Celery application for Cirrus."""

import os

from celery import Celery

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "cirrus.settings")

app = Celery("cirrus")
app.config_from_object("django.conf:settings", namespace="CELERY")
app.autodiscover_tasks()

# Explicitly include modules whose tasks live outside core/tasks.py
# (autodiscover_tasks() only finds <app>/tasks.py by convention).
app.conf.include = [
    "core.services.scheduler",
    "core.tasks_snowie",
    "core.cerebro_tasks",
    "core.tasks_api_keys",
]
