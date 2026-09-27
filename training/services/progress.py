from collections import defaultdict
from datetime import timedelta

from django.db.models import Avg
from django.utils import timezone

from training.models import FeedbackScore, Reflection, Scenario, TrainingSession
from training.services.boundary_scoring import RUBRIC_VERSION, conservative_median_score


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
                "average_score": conservative_median_score(observed_values),
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


def _mean(values):
    values = list(values)
    if not values:
        return None
    return round(sum(values) / len(values), 1)


def _session_completed_at(session):
    return session.end_time or session.start_time


def _format_day_month(value, include_year=False):
    suffix = f" {value.year}" if include_year else ""
    return f"{value.day} {value.strftime('%b')}{suffix}"


def _scores_by_session(completed_sessions):
    score_rows = FeedbackScore.objects.filter(
        feedback__session__in=completed_sessions,
        criterion__in=[criterion for criterion, _label, _key in SCORE_DIMENSIONS],
        observed=True,
        score__isnull=False,
        rubric_version=RUBRIC_VERSION,
    ).values("feedback__session_id", "criterion", "score")
    result = defaultdict(dict)
    for row in score_rows:
        result[row["feedback__session_id"]][row["criterion"]] = row["score"]
    return result


def _trainee_weighted_averages(session_rows, scores_by_session):
    user_scores = defaultdict(lambda: defaultdict(list))
    for session in session_rows:
        for criterion, score in scores_by_session.get(session.id, {}).items():
            user_scores[session.user_id][criterion].append(score)

    result = {}
    sample_sizes = {}
    for criterion, _label, key in SCORE_DIMENSIONS:
        trainee_means = [
            sum(scores[criterion]) / len(scores[criterion])
            for scores in user_scores.values()
            if scores.get(criterion)
        ]
        result[key] = _mean(trainee_means)
        sample_sizes[key] = len(trainee_means)
    return result, sample_sizes


def _dimension_progress_series(session_rows, scores_by_session, trainee_id=None):
    """Align progress by each boundary's observed attempt, not mixed session order."""
    ordered_sessions = sorted(
        session_rows,
        key=lambda item: (_session_completed_at(item), item.id),
    )
    series = []

    for criterion, label, key in SCORE_DIMENSIONS:
        observations_by_user = defaultdict(list)
        for session in ordered_sessions:
            score = scores_by_session.get(session.id, {}).get(criterion)
            if score is None:
                continue
            observations_by_user[session.user_id].append((session, score))

        cohort = []
        max_attempts = max(
            (len(observations) for observations in observations_by_user.values()),
            default=0,
        )
        for index in range(max_attempts):
            values = [
                observations[index][1]
                for observations in observations_by_user.values()
                if index < len(observations)
            ]
            sample_size = len(values)
            cohort.append(
                {
                    "attempt_number": index + 1,
                    "label": f"Attempt {index + 1}",
                    # 小样本均值不画趋势线，避免把单人结果误解为群体趋势。
                    "score": (
                        _mean(values)
                        if sample_size >= MIN_COHORT_TREND_SIZE
                        else None
                    ),
                    "sample_size": sample_size,
                }
            )

        personal = []
        if trainee_id is not None:
            for index, (session, score) in enumerate(
                observations_by_user.get(trainee_id, []),
                start=1,
            ):
                personal.append(
                    {
                        "attempt_number": index,
                        "label": f"Attempt {index}",
                        "score": score,
                        "session_id": session.id,
                        "completed_at": _session_completed_at(session).isoformat(),
                    }
                )

        has_cohort_data = any(point["score"] is not None for point in cohort)
        has_personal_data = bool(personal)
        series.append(
            {
                "key": key,
                "label": label,
                "cohort": cohort,
                "personal": personal,
                "has_cohort_data": has_cohort_data,
                "has_personal_data": has_personal_data,
                "has_visible_data": (
                    has_personal_data if trainee_id is not None else has_cohort_data
                ),
            }
        )

    return series


def _calendar_week_points(session_rows, trainee_id=None, week_count=8):
    today = timezone.localdate()
    current_monday = today - timedelta(days=today.weekday())
    first_monday = current_monday - timedelta(weeks=week_count - 1)
    selected_sessions = [
        session
        for session in session_rows
        if trainee_id is None or session.user_id == trainee_id
    ]
    points = []
    for week_index in range(week_count):
        start = first_monday + timedelta(weeks=week_index)
        end = start + timedelta(days=6)
        count = sum(
            1
            for session in selected_sessions
            if start <= timezone.localtime(_session_completed_at(session)).date() <= end
        )
        points.append(
            {
                "label": _format_day_month(start),
                "start": start.isoformat(),
                "end": end.isoformat(),
                "range_label": (
                    f"{_format_day_month(start)} - "
                    f"{_format_day_month(end, include_year=True)}"
                ),
                "sessions": count,
            }
        )
    return points


def admin_analytics_snapshot(selected_trainee=None):
    completed_sessions = list(
        TrainingSession.objects.filter(
            status=TrainingSession.Statuses.COMPLETED,
            user__role="TRAINEE",
        )
        .select_related("user", "scenario")
        .order_by("user_id", "end_time", "start_time", "id")
    )
    scores_by_session = _scores_by_session(completed_sessions)
    cohort_averages, cohort_sample_sizes = _trainee_weighted_averages(
        completed_sessions,
        scores_by_session,
    )
    selected_id = selected_trainee.id if selected_trainee else None
    scoped_sessions = (
        [
            session
            for session in completed_sessions
            if session.user_id == selected_trainee.id
        ]
        if selected_trainee
        else completed_sessions
    )
    scoped_averages, scoped_sample_sizes = _trainee_weighted_averages(
        scoped_sessions,
        scores_by_session,
    )
    contributing_trainees = {
        session.user_id
        for session in scoped_sessions
        if scores_by_session.get(session.id)
    }
    observed_session_count = sum(
        1 for session in scoped_sessions if scores_by_session.get(session.id)
    )
    progress_dimensions = _dimension_progress_series(
        completed_sessions,
        scores_by_session,
        trainee_id=selected_id,
    )
    if selected_trainee:
        max_visible_attempts = max(
            (len(dimension["personal"]) for dimension in progress_dimensions),
            default=0,
        )
    else:
        max_visible_attempts = max(
            (
                max(
                    (
                        point["attempt_number"]
                        for point in dimension["cohort"]
                        if point["score"] is not None
                    ),
                    default=0,
                )
                for dimension in progress_dimensions
            ),
            default=0,
        )
    raw_attempt_count = max(
        (len(dimension["cohort"]) for dimension in progress_dimensions),
        default=0,
    )

    return {
        "scope": "trainee" if selected_trainee else "cohort",
        "scope_label": (
            selected_trainee.full_name_or_email if selected_trainee else "All trainees"
        ),
        "total_sessions": len(scoped_sessions),
        "observed_session_count": observed_session_count,
        "effective_trainee_count": len(contributing_trainees),
        "averages": scoped_averages,
        "average_sample_sizes": (
            scoped_sample_sizes if selected_trainee else cohort_sample_sizes
        ),
        "progress_chart": {
            "mode": "trainee" if selected_trainee else "cohort",
            "dimensions": progress_dimensions,
            "max_attempts": max_visible_attempts,
            "has_observations": raw_attempt_count > 0,
            "has_visible_data": max_visible_attempts > 0,
            "minimum_cohort_size": MIN_COHORT_TREND_SIZE,
            "has_cohort_benchmark": any(
                dimension["has_cohort_data"] for dimension in progress_dimensions
            ),
        },
        "activity_chart": _calendar_week_points(
            completed_sessions,
            trainee_id=selected_id,
        ),
    }
