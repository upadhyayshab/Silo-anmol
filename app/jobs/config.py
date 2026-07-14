import os
from apscheduler.triggers.interval import IntervalTrigger
from apscheduler.triggers.cron import CronTrigger
from services.smartping_job_service import smartping_job_service
from services.inventory_audit_service import inventory_audit_service
from services import facebook_service
from services.leadService import sweep_unassigned
from services import order_aging_service

# Define your interval or cron schedules here.

JOBS_CONFIG = [
    {
        "name": "dispatch_smartping_jobs",
        "func": smartping_job_service.dispatch_due_jobs,
        "trigger": IntervalTrigger(minutes=1)
    },
    {
        # Pick up leads that arrived while everyone was offline (e.g. overnight) and
        # hand them to online telecallers. distribute_leads enforces online + quota.
        "name": "assign_unassigned_leads",
        "func": sweep_unassigned,
        "trigger": IntervalTrigger(minutes=5)
    },
    {
        # Audit cycle opens Saturday; outlets fill through the following Wednesday.
        # Scheduler runs on Asia/Kolkata now (see scheduler.py) — these hours are IST
        # directly. Was hour=0,minute=20 UTC (= Sat 05:50 IST) when the scheduler ran
        # on the container's implicit UTC clock; re-expressed here to fire at the
        # same real-world instant, not a new time.
        "name": "generate_weekly_inventory_audits",
        "func": inventory_audit_service.generate_weekly_audits,
        "trigger": CronTrigger(day_of_week='sat', hour=5, minute=50)
    },
    {
        # Close the cycle late on its Wednesday deadline — any still-PENDING audit
        # becomes CLOSED (missed) and can no longer be submitted.
        # Was hour=23,minute=30 UTC Wed (= Thu 05:00 IST); re-expressed to fire at
        # the same real-world instant.
        "name": "close_overdue_inventory_audits",
        "func": inventory_audit_service.close_overdue_audits,
        "trigger": CronTrigger(day_of_week='thu', hour=5, minute=0)
    },
    {
        # Re-discover Facebook pages nightly and subscribe any new ones (+ backfill them).
        # Was hour=2,minute=0 UTC (= 07:30 IST); re-expressed to fire at the same
        # real-world instant.
        "name": "facebook_sync_pages",
        "func": facebook_service.nightly_sync,
        "trigger": CronTrigger(hour=7, minute=30)
    },
    {
        # Nightly 30-day auto-cancel for escalated, unresolved orders. Kill-switch
        # flagged (SETTING_AGING_CANCEL_ENABLED, default off) — see order_aging_service.
        "name": "cancel_aged_orders",
        "func": order_aging_service.cancel_aged_orders,
        "trigger": CronTrigger(hour=0, minute=30)   # 00:30 IST
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
