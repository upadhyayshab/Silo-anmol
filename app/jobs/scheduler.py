from apscheduler.schedulers.asyncio import AsyncIOScheduler
from .config import JOBS_CONFIG

# Initialize fully async scheduler (defaults to MemoryJobStore)
scheduler_app = AsyncIOScheduler()

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
