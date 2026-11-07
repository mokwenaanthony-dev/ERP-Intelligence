"""Celery application: background and scheduled work (analytics refresh, graph projection).

Workers load Django settings, discover each app's ``tasks.py`` and run the schedule in
``settings.CELERY_BEAT_SCHEDULE``. While a task runs, its log lines carry ``task:<task id>`` as their
request ID (see ``apps/common/logcontext.py``), so one task's lines can be found together, and a
failing task is logged with its full traceback.
"""
import logging
import os

from celery import Celery
from celery.signals import task_failure, task_postrun, task_prerun

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "erp_system.settings")

app = Celery("erp_system")
app.config_from_object("django.conf:settings", namespace="CELERY")
app.autodiscover_tasks()

logger = logging.getLogger("erp.tasks")
_tokens = {}


@task_prerun.connect
def _start(task_id=None, task=None, **kwargs):
    from apps.common.logcontext import request_id

    _tokens[task_id] = request_id.set(f"task:{(task_id or '')[:8]}")
    logger.debug("Task %s started", task.name if task else "?")


@task_postrun.connect
def _finish(task_id=None, task=None, state=None, **kwargs):
    from apps.common.logcontext import request_id

    logger.debug("Task %s finished: %s", task.name if task else "?", state)
    token = _tokens.pop(task_id, None)
    if token is not None:
        request_id.reset(token)


@task_failure.connect
def _failed(task_id=None, exception=None, sender=None, **kwargs):
    from apps.common.logcontext import error_location

    logger.error("Task %s failed: %s (raised at %s)", getattr(sender, "name", "?"), exception,
                 error_location(exception) if exception else "unknown location", exc_info=exception)
