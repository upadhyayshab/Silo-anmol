from datetime import datetime


def build_cumulative_remarks(previous_records, current_status, current_remark):
    """
    Builds a running timestamped log of all delivery status changes.
    Each new tracking record stores the full history in its remarks field.
    """
    lines = []
    for r in previous_records:
        ts = r.created_at.strftime("%Y-%m-%d %H:%M UTC") if r.created_at else "?"
        status_val = r.status_changed_to.value if hasattr(r.status_changed_to, "value") else str(r.status_changed_to)
        lines.append(f"[{ts}] {status_val}: {r.remarks or '-'}")
    ts_now = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
    status_val = current_status.value if hasattr(current_status, "value") else str(current_status)
    lines.append(f"[{ts_now}] {status_val}: {current_remark or '-'}")
    return "\n".join(lines)
