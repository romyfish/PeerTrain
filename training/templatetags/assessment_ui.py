"""Trainee-facing assessment presentation; internal scoring stays unchanged."""

from django import template

register = template.Library()

_STATUSES = {
    1: ("Needs attention", "Needs improvement", "attention", "needs-attention"),
    2: ("Finding balance", "Keep practising", "balance", "finding-balance"),
    3: ("Making progress", "Good progress", "progress", "making-progress"),
    4: ("Well handled", "Well done", "handled", "well-handled"),
    5: ("Excellent handling", "Excellent work", "excellent", "excellent-handling"),
}


@register.simple_tag
def assessment_status(score, overall=False):
    live_label, overall_label, tone, icon = _STATUSES.get(
        score,
        ("Listening in progress", "Not observed", "neutral", "listening-in-progress"),
    )
    return {
        "label": overall_label if overall else live_label,
        "tone": tone,
        "icon_asset": f"images/boundary-status/{icon}.svg",
    }
