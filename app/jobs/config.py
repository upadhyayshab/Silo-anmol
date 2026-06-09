import os
from apscheduler.triggers.interval import IntervalTrigger
from apscheduler.triggers.cron import CronTrigger
from services.smartping_job_service import smartping_job_service

# Define your interval or cron schedules here.

JOBS_CONFIG = [
    {
        "name": "dispatch_smartping_jobs",
        "func": smartping_job_service.dispatch_due_jobs,
        "trigger": IntervalTrigger(minutes=1)
    },
    # Examples of other schedules you can easily add:
    # {
    #     "name": "run_daily_reports",
    #     "func": generate_reports_function,
    #     "trigger": CronTrigger(hour=23, minute=30)  # Runs exactly at 11:30 PM
    # },
    # {
    #     "name": "custom_cron_job",
    #     "func": some_custom_function,
    #     "trigger": CronTrigger.from_crontab("0 2 * * *")  # Runs at 2:00 AM every day
    # }
]
