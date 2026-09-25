from celery import Celery

from app.core.config import get_settings

settings = get_settings()
celery_app = Celery(
    "taskmate", broker=settings.redis_url, backend=settings.redis_url, include=["app.workers.tasks"]
)
celery_app.conf.update(
    task_always_eager=settings.celery_task_always_eager,
    task_eager_propagates=True,
    timezone="UTC",
    beat_schedule={
        "schedule-tick": {
            "task": "app.workers.tasks.schedule_tick",
            "schedule": settings.scheduler_tick_interval_seconds,
        },
        "cleanup-actions": {"task": "app.workers.tasks.cleanup_expired", "schedule": 3600},
    },
)
