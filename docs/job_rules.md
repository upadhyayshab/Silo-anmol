# Background Job Rules & Architecture

This document explains the architecture for adding and managing scheduled background jobs within the ERP system.

We use **Rocketry** as our fully asynchronous execution engine, driven by a centralized configuration.

## The Architecture Flow: `Service -> Config -> Scheduler`

To keep our application modular and highly performant, we follow a strict 3-step flow for any new background job:

1. **Service (func)**: Write the actual business logic inside the appropriate `services/` file. The function must be `async`.
2. **Config**: Import that function into `app/jobs/config.py` and map it to a specific schedule (interval, time, or cron).
3. **Scheduler**: The system automatically reads the config on startup and schedules it inside the FastAPI event loop.

---

## Step 1: Write the Service (The Function)

Do not write heavy business logic inside the `jobs/` folder. All logic should live in the `app/services/` layer.

**Rules for the function:**

- It **must** be an `async` function.
- It should handle its own exceptions (so it doesn't crash the scheduler).
- It should not require mandatory positional arguments.

_Example (`app/services/report_service.py`):_

```python
async def generate_daily_report():
    try:
        print("Generating report...")
        # Your logic here
    except Exception as e:
        print(f"Report generation failed: {e}")
```

---

## Step 2: Add to Config (`app/jobs/config.py`)

Open `app/jobs/config.py`. This file is the single Source of Truth for all background tasks.

Import your service function and add a dictionary block to the `JOBS_CONFIG` array.

```python
from services.report_service import generate_daily_report
from rocketry.conds import daily

JOBS_CONFIG = [
    # ... existing jobs ...
    {
        "name": "daily_report_generator",
        "func": generate_daily_report,
        "condition": daily.at("23:30")  # Schedule it here
    }
]
```

### Common Scheduling Conditions (Rocketry)

Import the conditions you need from `rocketry.conds`:

**Intervals:**

```python
from rocketry.conds import every

"condition": every("1 minute")
"condition": every("2 hours")
"condition": every("3 days")
```

**Exact Times:**

```python
from rocketry.conds import daily, hourly, weekly

"condition": daily.at("14:30")       # 2:30 PM every day
"condition": hourly.at("45:00")      # At the 45th minute of every hour
"condition": weekly.on("Monday")     # Every Monday
```

**Cron Expressions:**

```python
from rocketry.conds import cron

"condition": cron("0 2 * * *")       # Standard cron string (2:00 AM daily)
```

---

## Step 3: The Scheduler

You do not need to modify `app/jobs/scheduler.py` or `app.py`!

The `scheduler.py` module is designed to dynamically read `JOBS_CONFIG` and inject the tasks into the `Rocketry` app when the FastAPI server starts. Just write your service, update the config, and the scheduler handles the rest automatically.
