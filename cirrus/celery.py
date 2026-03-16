"""Celery application for Cirrus."""

import os

from celery import Celery

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "cirrus.settings")

app = Celery("cirrus")
app.config_from_object("django.conf:settings", namespace="CELERY")
app.autodiscover_tasks()

# Explicitly include scheduler tasks (lives in core/services/scheduler.py,
# not core/tasks.py, so autodiscover_tasks() won't find it)
app.conf.include = ["core.services.scheduler"]
