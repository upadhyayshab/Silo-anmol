import os
import pytz
from apscheduler.triggers.interval import IntervalTrigger
from apscheduler.triggers.cron import CronTrigger
from services.smartping_job_service import smartping_job_service
from services.inventory_audit_service import inventory_audit_service
from services import facebook_service
from services.leadService import sweep_unassigned
from services.order_revert_service import revert_stale_orders

IST = pytz.timezone("Asia/Kolkata")

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
        "name": "generate_weekly_inventory_audits",
        "func": inventory_audit_service.generate_weekly_audits,
        "trigger": CronTrigger(day_of_week='sat', hour=0, minute=20)
    },
    {
        # Close the cycle late on its Wednesday deadline — any still-PENDING audit
        # becomes CLOSED (missed) and can no longer be submitted.
        "name": "close_overdue_inventory_audits",
        "func": inventory_audit_service.close_overdue_audits,
        "trigger": CronTrigger(day_of_week='wed', hour=23, minute=30)
    },
    {
        # Re-discover Facebook pages nightly and subscribe any new ones (+ backfill them).
        "name": "facebook_sync_pages",
        "func": facebook_service.nightly_sync,
        "trigger": CronTrigger(hour=2, minute=0)
    },
    {
        # Revert stale (non-terminal, non-pending) orders to PENDING once a day at
        # midnight IST. No-op unless a super admin has enabled it (system_configuration).
        "name": "revert_stale_orders",
        "func": revert_stale_orders,
        "trigger": CronTrigger(hour=0, minute=0, timezone=IST)
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
