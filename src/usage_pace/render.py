"""Human-readable progress bars, shared by all providers."""

from datetime import datetime
from typing import Any

STATUS_VISUALS = {
    "below": ("🟢", "BELOW PACE"),
    "on track": ("🟡", "ON PACE"),
    "above": ("🔴", "ABOVE PACE"),
    "unavailable": ("⚪", "PACE UNAVAILABLE"),
}


def labeled_bar(label: str, percent: float) -> str:
    filled = round(max(0, min(100, percent)) / 5)
    return f"{label:<15} {percent:>5.1f}%  {'█' * filled}{'░' * (20 - filled)}"


def render_windows(reports: list[dict[str, Any]]) -> str:
    blocks = []
    for report in reports:
        icon, headline = STATUS_VISUALS[report["status"]]
        detail = ""
        if report["status"] == "on track":
            detail = f" — within ±{report['tolerance_pp']:.1f} percentage points"
        elif report["delta_percent"] is not None:
            detail = f" — {abs(report['delta_percent']):.1f} percentage points"
        lines = [report["label"], f"{icon} {headline}{detail}"]
        if report["resets_at"]:
            reset = datetime.fromisoformat(report["resets_at"]).astimezone()
            hours = report["seconds_remaining"] / 3600
            left = f"{hours / 24:.1f} days" if hours >= 24 else f"{hours:.1f} hours"
            lines.append(f"Resets {reset:%Y-%m-%d %H:%M %Z} ({left} left)")
        if report["expected_percent"] is not None:
            lines.append(labeled_bar("Window elapsed", report["expected_percent"]))
        lines.append(labeled_bar("Usage", report["usage_percent"]))
        for label, percent in report.get("breakdown", {}).items():
            lines.append(labeled_bar(label, percent))
        if report["projected_percent"] is not None:
            lines.append(
                f"At this window's average pace: {report['projected_percent']:.1f}% at reset"
            )
        if report["pace_note"]:
            lines.append(report["pace_note"])
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)
