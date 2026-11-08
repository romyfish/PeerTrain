from datetime import timedelta

from django.db.models import Avg
from django.utils import timezone

from training.models import FeedbackScore, Reflection, Scenario, TrainingSession


SCORE_DIMENSIONS = (
    (FeedbackScore.Criteria.AGENCY, "Agency Boundary", "agency"),
    (
        FeedbackScore.Criteria.RELATIONSHIP,
        "Relationship Boundary",
        "relationship",
    ),
    (
        FeedbackScore.Criteria.SAFETY,
        "Safety Boundary",
        "safety",
    ),
)

MIN_COHORT_TREND_SIZE = 3

CORE_SCENARIO_TYPES = (
    Scenario.BoundaryTypes.AGENCY,
    Scenario.BoundaryTypes.RELATIONSHIP,
    Scenario.BoundaryTypes.SAFETY,
)


def _completed_sessions(user):
    return (
        TrainingSession.objects.filter(
            user=user,
            status=TrainingSession.Statuses.COMPLETED,
        )
        .select_related("scenario")
        .prefetch_related("feedback__scores", "feedback__review_items")
        .order_by("-end_time", "-start_time", "-id")
    )


def _current_streak(sessions):
    today = timezone.localdate()
    session_days = sorted(
        {timezone.localtime(session.start_time).date() for session in sessions},
        reverse=True,
    )
    streak = 0
    cursor = today
    for day in session_days:
        if day == cursor:
            streak += 1
            cursor -= timedelta(days=1)
        elif day < cursor:
            break
    return streak


def _xp_summary(sessions, reflection_session_ids):
    xp = len(sessions) * 20
    xp += len(reflection_session_ids) * 10
    completed_core = {
        session.scenario.boundary_type
        for session in sessions
        if session.scenario.boundary_type in CORE_SCENARIO_TYPES
    }
    xp += len(completed_core) * 10
    level = (xp // 100) + 1
    return {
        "xp": xp,
        "xp_level": level,
        "xp_into_level": xp % 100,
        "xp_to_next_level": 100 - (xp % 100) if xp % 100 else 100,
    }


def _score_rows_for_session(session):
    if not hasattr(session, "feedback"):
        return {}
    return {score.criterion: score for score in session.feedback.scores.all()}


def recalculate_user_progress(user):
    sessions = list(_completed_sessions(user))
    observed_scores = FeedbackScore.objects.filter(
        feedback__session__user=user,
        observed=True,
        score__isnull=False,
    )
    averages = observed_scores.values("criterion").annotate(average=Avg("score"))
    reflection_ids = set(
        Reflection.objects.filter(session__user=user).values_list("session_id", flat=True)
    )
    summary = _xp_summary(sessions, reflection_ids)
    summary.update(
        {
            "completed_sessions": len(sessions),
            "average_overall": round(
                sum(
                    session.overall_score
                    for session in sessions
                    if session.overall_score is not None
                )
                / max(
                    1,
                    len(
                        [
                            session
                            for session in sessions
                            if session.overall_score is not None
                        ]
                    ),
                ),
                1,
            ),
            "average_boundary_score": round(
                observed_scores.aggregate(average=Avg("score"))["average"] or 0,
                1,
            ),
            "average_scores": {
                row["criterion"]: round(row["average"] or 0, 1)
                for row in averages
            },
            "awarded": [],
        }
    )
    return summary


def progress_snapshot(user):
    sessions = list(_completed_sessions(user))
    reflection_ids = set(
        Reflection.objects.filter(session__user=user).values_list("session_id", flat=True)
    )
    observed_scores = FeedbackScore.objects.filter(
        feedback__session__user=user,
        observed=True,
        score__isnull=False,
    )
    by_criterion = {
        row["criterion"]: round(row["average"] or 0, 1)
        for row in observed_scores.values("criterion").annotate(average=Avg("score"))
    }

    # 学员趋势按每个边界自己的真实观察次数排列，避免把未观察维度连接到混合会话轴。
    ordered_sessions = list(reversed(sessions))
    trend_colours = {
        "agency": "#4285f4",
        "relationship": "#f29900",
        "safety": "#0f9d8a",
    }
    trend_shapes = {
        "agency": "circle",
        "relationship": "rectRounded",
        "safety": "triangle",
    }
    skill_trends = []
    for criterion, label, key in SCORE_DIMENSIONS:
        observations = []
        for session in ordered_sessions:
            score = _score_rows_for_session(session).get(criterion)
            if not score or not score.observed or score.score is None:
                continue
            completed_at = timezone.localtime(session.end_time or session.start_time)
            observations.append(
                {
                    "attempt": len(observations) + 1,
                    "score": score.score,
                    "session_id": session.id,
                    "scenario": session.scenario.title,
                    "date": completed_at.strftime("%d %b %Y"),
                }
            )

        latest_score = observations[-1]["score"] if observations else None
        previous_score = observations[-2]["score"] if len(observations) > 1 else None
        change = (
            latest_score - previous_score if previous_score is not None else None
        )
        skill_trends.append(
            {
                "criterion": criterion,
                "label": label,
                "key": key,
                "colour": trend_colours[key],
                "point_style": trend_shapes[key],
                "points": observations[-5:],
                "latest_score": latest_score,
                "change": change,
                "change_magnitude": abs(change) if change is not None else None,
                "change_direction": (
                    "up"
                    if change and change > 0
                    else "down"
                    if change and change < 0
                    else ""
                ),
                "total_observations": len(observations),
            }
        )

    recent_rows = []
    for session in sessions[:8]:
        scores = _score_rows_for_session(session)
        observed_values = [
            score.score
            for score in scores.values()
            if score.observed and score.score is not None
        ]
        recent_rows.append(
            {
                "id": session.id,
                "date": timezone.localtime(session.end_time or session.start_time).strftime(
                    "%d %b %Y"
                ),
                "scenario": session.scenario.title,
                "average_score": sorted(observed_values)[(len(observed_values)-1)//2] if observed_values else None,
                "has_reflection": session.id in reflection_ids,
                "ended_early": session.ended_early,
            }
        )

    snapshot = {
        "completed_sessions": len(sessions),
        "average_score": round(
            sum(
                session.overall_score
                for session in sessions
                if session.overall_score is not None
            )
            / max(
                1,
                len(
                    [
                        session
                        for session in sessions
                        if session.overall_score is not None
                    ]
                ),
            ),
            1,
        ),
        "current_streak": _current_streak(sessions),
        "average_boundary_score": round(
            observed_scores.aggregate(average=Avg("score"))["average"] or 0,
            1,
        ),
        "skill_trends": skill_trends,
        "has_skill_trends": any(trend["points"] for trend in skill_trends),
        "skill_breakdown": [
            {
                "criterion": criterion,
                "label": label,
                "key": key,
                "score": by_criterion.get(criterion),
            }
            for criterion, label, key in SCORE_DIMENSIONS
        ],
        "recent_rows": recent_rows,
    }
    snapshot.update(_xp_summary(sessions, reflection_ids))
    return snapshot


def dashboard_snapshot(user):
    return progress_snapshot(user)
