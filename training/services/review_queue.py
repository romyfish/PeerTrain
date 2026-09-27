import hashlib
from collections import defaultdict

from django.conf import settings
from django.db import transaction

from training.models import BoundaryEvaluationRun, BoundaryTurnEvaluation
from training.services.boundary_scoring import conservative_median_score


REVIEW_REASON_CODES = {
    BoundaryTurnEvaluation.ReviewReasons.LOW_CONFIDENCE,
    BoundaryTurnEvaluation.ReviewReasons.SCORE_DISAGREEMENT,
    BoundaryTurnEvaluation.ReviewReasons.BASELINE_NOT_OBSERVED,
    BoundaryTurnEvaluation.ReviewReasons.CRITICAL_UNCONFIRMED,
}


def _resolution_detail(run, boundary_key):
    details = run.resolution_details or {}
    return details.get(boundary_key) or details.get(boundary_key.lower()) or {}


def _quality_sample_selected(run, boundary_key):
    rate = float(getattr(settings, "BOUNDARY_JUDGE_REVIEW_SAMPLE_RATE", 0.10))
    rate = min(max(rate, 0.0), 1.0)
    if rate <= 0:
        return False
    if rate >= 1:
        return True
    sample_key = f"{run.session_id}:{run.id}:{boundary_key}".encode("utf-8")
    bucket = int.from_bytes(hashlib.sha256(sample_key).digest()[:8], "big")
    return bucket / float(2**64) < rate


def _review_selection_for_boundary(run, boundary_key, evaluations):
    if run.verification_of_id:
        return (
            BoundaryTurnEvaluation.ReviewScopes.NONE,
            "",
        )

    detail = _resolution_detail(run, boundary_key)
    if detail.get("decision") == "REVIEW_REQUIRED":
        reason = detail.get("reason_code", "")
        if reason not in REVIEW_REASON_CODES:
            reason = BoundaryTurnEvaluation.ReviewReasons.RELIABILITY_WARNING
        return (
            BoundaryTurnEvaluation.ReviewScopes.REQUIRED,
            reason,
        )

    if any(item.critical_concern or item.score == 1 for item in evaluations):
        return (
            BoundaryTurnEvaluation.ReviewScopes.REQUIRED,
            BoundaryTurnEvaluation.ReviewReasons.CRITICAL_CONCERN,
        )

    if _quality_sample_selected(run, boundary_key):
        return (
            BoundaryTurnEvaluation.ReviewScopes.QUALITY_SAMPLE,
            BoundaryTurnEvaluation.ReviewReasons.QUALITY_SAMPLE,
        )

    return (
        BoundaryTurnEvaluation.ReviewScopes.NONE,
        "",
    )


@transaction.atomic
def configure_review_queue_for_run(run):
    evaluations = list(run.turn_evaluations.order_by("id"))
    by_boundary = defaultdict(list)
    for evaluation in evaluations:
        by_boundary[evaluation.boundary_key].append(evaluation)

    has_pending_required = False
    for boundary_key, boundary_evaluations in by_boundary.items():
        scope, reason = _review_selection_for_boundary(
            run,
            boundary_key,
            boundary_evaluations,
        )
        for evaluation in boundary_evaluations:
            if evaluation.review_status in {
                BoundaryTurnEvaluation.ReviewStatuses.CONFIRMED,
                BoundaryTurnEvaluation.ReviewStatuses.CORRECTED,
                BoundaryTurnEvaluation.ReviewStatuses.REJECTED,
            }:
                if evaluation.review_scope == BoundaryTurnEvaluation.ReviewScopes.NONE:
                    evaluation.review_scope = (
                        BoundaryTurnEvaluation.ReviewScopes.REQUIRED
                    )
                    evaluation.review_reason = (
                        BoundaryTurnEvaluation.ReviewReasons.MANUAL_SELECTION
                    )
            else:
                evaluation.review_scope = scope
                evaluation.review_reason = reason
                evaluation.review_status = (
                    BoundaryTurnEvaluation.ReviewStatuses.PENDING
                    if scope != BoundaryTurnEvaluation.ReviewScopes.NONE
                    else BoundaryTurnEvaluation.ReviewStatuses.NOT_REQUIRED
                )
            evaluation.save(
                update_fields=[
                    "review_scope",
                    "review_reason",
                    "review_status",
                ]
            )
            if (
                evaluation.review_scope
                == BoundaryTurnEvaluation.ReviewScopes.REQUIRED
                and evaluation.review_status
                == BoundaryTurnEvaluation.ReviewStatuses.PENDING
            ):
                has_pending_required = True

    if has_pending_required:
        run.production_ready = False
        run.resolution_status = (
            BoundaryEvaluationRun.ResolutionStatuses.REVIEW_REQUIRED
        )
        run.save(update_fields=["production_ready", "resolution_status"])
    return run


def grouped_review_queue(queryset):
    groups = {}
    for evaluation in queryset:
        key = (evaluation.run_id, evaluation.boundary_key)
        group = groups.setdefault(
            key,
            {
                "run": evaluation.run,
                "boundary_key": evaluation.boundary_key,
                "boundary_label": evaluation.get_boundary_key_display(),
                "scope": evaluation.review_scope,
                "scope_label": evaluation.get_review_scope_display(),
                "reason": evaluation.review_reason,
                "reason_label": evaluation.get_review_reason_display(),
                "evaluations": [],
                "pending_count": 0,
                "minimum_confidence": None,
                "has_critical": False,
            },
        )
        group["evaluations"].append(evaluation)
        if evaluation.review_status == BoundaryTurnEvaluation.ReviewStatuses.PENDING:
            group["pending_count"] += 1
        if evaluation.applicable:
            current = group["minimum_confidence"]
            group["minimum_confidence"] = (
                evaluation.confidence
                if current is None
                else min(current, evaluation.confidence)
            )
        group["has_critical"] = (
            group["has_critical"]
            or evaluation.critical_concern
            or evaluation.score == 1
        )

    rows = list(groups.values())
    for row in rows:
        boundary_key = row["boundary_key"]
        run = row["run"]
        row["judge_score"] = (run.aggregate_scores or {}).get(boundary_key)
        row["baseline_score"] = (run.baseline_rule_scores or {}).get(boundary_key)
        row["resolved_score"] = (run.resolved_scores or {}).get(boundary_key)
        row["is_pending"] = row["pending_count"] > 0
    return rows


@transaction.atomic
def apply_human_review_resolution(run, boundary_key):
    selected = list(
        run.turn_evaluations.filter(
            boundary_key=boundary_key,
        )
        .exclude(review_scope=BoundaryTurnEvaluation.ReviewScopes.NONE)
        .order_by("trainee_turn_number", "id")
    )
    if not selected:
        return run
    if any(
        item.review_status == BoundaryTurnEvaluation.ReviewStatuses.PENDING
        for item in selected
    ):
        return run

    human_scores = [
        item.effective_score
        for item in selected
        if item.applicable and item.effective_score is not None
    ]
    human_score = conservative_median_score(human_scores)
    baseline_score = (run.baseline_rule_scores or {}).get(boundary_key)
    resolved_score = human_score if human_score is not None else baseline_score

    statuses = {item.review_status for item in selected}
    if BoundaryTurnEvaluation.ReviewStatuses.CORRECTED in statuses:
        reason_code = "HUMAN_CORRECTED"
        decision = "Human correction applied"
    elif BoundaryTurnEvaluation.ReviewStatuses.REJECTED in statuses:
        reason_code = "HUMAN_REVIEWED_WITH_REJECTIONS"
        decision = "Human review applied with rejected rows"
    else:
        reason_code = "HUMAN_CONFIRMED"
        decision = "Human reviewer confirmed the judge evidence"

    resolved_scores = dict(run.resolved_scores or {})
    resolved_scores[boundary_key] = resolved_score
    details = dict(run.resolution_details or {})
    original_detail = _resolution_detail(run, boundary_key)
    details[boundary_key] = {
        **original_detail,
        "decision": "HUMAN_REVIEWED",
        "reason_code": reason_code,
        "reason": decision,
        "human_score": human_score,
        "resolved_score": resolved_score,
        "reviewed_evaluation_ids": [item.id for item in selected],
    }

    pending_required = run.turn_evaluations.filter(
        review_scope=BoundaryTurnEvaluation.ReviewScopes.REQUIRED,
        review_status=BoundaryTurnEvaluation.ReviewStatuses.PENDING,
    ).exists()
    run.resolved_scores = resolved_scores
    run.resolution_details = details
    if run.status == BoundaryEvaluationRun.Statuses.SUCCEEDED:
        run.production_ready = not pending_required
        run.resolution_status = (
            BoundaryEvaluationRun.ResolutionStatuses.REVIEW_REQUIRED
            if pending_required
            else BoundaryEvaluationRun.ResolutionStatuses.ACCEPTED
        )
    run.save(
        update_fields=[
            "resolved_scores",
            "resolution_details",
            "production_ready",
            "resolution_status",
        ]
    )
    return run
