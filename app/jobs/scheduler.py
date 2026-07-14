from apscheduler.schedulers.asyncio import AsyncIOScheduler
from .config import JOBS_CONFIG

# Initialize fully async scheduler (defaults to MemoryJobStore). Explicit IST
# timezone so CronTrigger hour/minute values in config.py mean IST wall-clock
# directly, instead of implicitly depending on the container's system tz
# (which happens to be UTC today, but was never guaranteed).
scheduler_app = AsyncIOScheduler(timezone="Asia/Kolkata")

# Dynamically register all tasks from the config file
def load_jobs():
    for job in JOBS_CONFIG:
        # Wrap async functions properly and register them
        scheduler_app.add_job(
            job["func"],
            trigger=job["trigger"],
            id=job["name"],
            replace_existing=True
        )

# Load the jobs immediately when this module is imported
load_jobs()
