from celery import Celery
from src.backend.config import settings
from celery.schedules import crontab

celery_app = Celery(
    "devguard",
    broker=settings.CELERY_BROKER_URL,
    backend=settings.BACKEND_URL,
    include=["src.backend.tasks.security_scans"]
)


#celery config
celery_app.conf.update(
    # Serialization
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="UTC",
    enable_utc=True,
    
    # Task execution
    task_track_started=True,
    task_time_limit=30 * 60,  # 30 minutes
    task_soft_time_limit=25 * 60, # 25 minutes
    
    # Task retry
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    task_default_retry_delay=60,
    task_max_retries=3,
    
    # Worker
    worker_prefetch_multiplier=1,
    worker_max_tasks_per_child=100,
    
    # Results
    result_expires=60 * 60 * 24 * 3,  # 3 days
    
    # cron jobs
    beat_schedule={
        "scan-all-projects-daily": {
            "task": "src.backend.tasks.security_scans.scan_all_projects",
            "schedule": crontab(hour=2, minute=0),
            "args": (),
        },
        "cleanup-old-results": {
            "task": "src.backend.tasks.cleanup_old_results",
            "schedule": crontab(hour=3, minute=0),
            "args": (),
        },
        "check-cost-alerts": {
            "task": "src.backend.tasks.cost_estimation.check_cost_alerts",
            "schedule": crontab(minute="*/30"),
            "args": (),
        },
    },
)

# Auto-discover tasks
celery_app.autodiscover_tasks(["src.backend.tasks"])
