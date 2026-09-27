import logging
import re
import unicodedata
from dataclasses import dataclass, replace
from difflib import SequenceMatcher
from typing import Any

from django.conf import settings
from google import genai
from google.genai import types as google_types
from openai import OpenAI
from pydantic import BaseModel, Field

from training.content import (
    AUTO_TRAINEE_REPLY_FALLBACKS,
    AUTO_TRAINEE_REPLIES,
    BOUNDARY_DIMENSION_ORDER,
    PLACEHOLDER_REPLY_VARIANTS,
    PLACEHOLDER_IMPROVEMENTS,
    PLACEHOLDER_REPLIES,
    PLACEHOLDER_STRENGTHS,
    ROLEPLAY_ANTI_PATTERNS,
    ROLEPLAY_REALISM_RULES,
    auto_trainee_reply_for,
    boundary_framework_for,
    boundary_framework_for_dimension,
    conversation_stage_for,
    default_prompt_parts,
    opening_message_for,
    scenario_prompt_context_for,
    scenario_progress_step,
    scenario_roleplay_brief,
    session_persona_for,
)
from training.models import (
    BoundaryEvaluationRun,
    BoundaryTurnEvaluation,
    Feedback,
    FeedbackReviewItem,
    FeedbackScore,
    Message,
    Prompt,
    ReflectionGuidance,
    Scenario,
)
from training.rubrics.boundaries import BOUNDARY_RUBRICS
from training.services.boundary_scoring import (
    BoundaryProcessStepResult,
    OUTCOME_LABELS,
    RUBRIC_VERSION,
    classify_relationship_response,
    conservative_median_score,
    evaluate_boundary_conversation,
    evaluate_boundary_session,
    prompt_rubric_contract,
)
from training.services.geval import (
    EVIDENCE_RUBRIC_VERSION,
    GEVAL_PROMPT_VERSION,
    GEVAL_VERSION,
    aggregate_persisted_geval_boundary,
    ensure_reliable_geval_run,
    latest_resolved_geval_run,
)

logger = logging.getLogger(__name__)

DEFAULT_RESPONSE_LANGUAGE = "English (UK)"
ROLEPLAY_MAX_OUTPUT_TOKENS = 320
ROLEPLAY_REPAIR_ATTEMPTS = 2
DEMO_TRAINEE_MAX_OUTPUT_TOKENS = 220
DEMO_TRAINEE_REPAIR_ATTEMPTS = 2
PRODUCTION_SCORING_VERSION = "HYBRID-BOUNDARY-1.0"
JUDGE_ACCEPTED_DECISIONS = {"LLM_ACCEPTED", "LLM_CONSENSUS_ACCEPTED"}
REVIEW_REQUIRED_RATIONALE = (
    "Boundary-specific feedback was identified, but the protected scorer could not "
    "confirm enough reliable evidence to assign a level. This dimension needs review."
)


class BoundaryScorePayload(BaseModel):
    boundary_key: str
    observed: bool
    score: int | None = Field(default=None, ge=1, le=5)
    evidence: str = ""
    rationale: str = ""
    next_move: str = ""


class FeedbackPayload(BaseModel):
    boundary_scores: list[BoundaryScorePayload]
    what_worked: list[str]
    next_boundary_moves: list[str]
    review_items: list["FeedbackReviewItemPayload"] = Field(default_factory=list)


class FeedbackReviewItemPayload(BaseModel):
    trainee_turn_number: int = Field(ge=1, le=20)
    boundary_key: str = FeedbackReviewItem.BoundaryKeys.AGENCY
    issue_label: str
    why_it_matters: str
    better_reply: str


FeedbackPayload.model_rebuild()


class ReflectionGuidancePayload(BaseModel):
    notice_prompt: str
    action_prompt: str


@dataclass(frozen=True)
class GeneratedTraineeReply:
    content: str
    response_source: str
    used_fallback: bool = False
    fallback_reason: str = ""
    fallback_notice: str = ""


@dataclass(frozen=True)
class RoleplayRuntimeBrief:
    stage_label: str
    stage_description: str
    stage_objective: str
    stage_disclosure: str
    empathy_score: int
    exploration_score: int
    boundary_score: int
    directive_score: int
    overpromise_score: int
    referral_score: int
    disclosure_instruction: str
    emotional_shift: str
    target_pressure_instruction: str
    risk_guidance: str
    relationship_state: str = ""


@dataclass(frozen=True)
class BoundaryStatusIndicator:
    score: int | None
    tone: str
    label: str
    headline: str
    summary: str
    cue: str


@dataclass(frozen=True)
class BoundaryDimensionIndicator:
    key: str
    name: str
    definition: str
    score: int | None
    tone: str
    label: str
    summary: str
    observed: bool
    rubric_version: str
    evidence_met: tuple[str, ...]
    evidence_missing: tuple[str, ...]
    major_concerns: tuple[str, ...]
    critical_breaches: tuple[str, ...]
    evidence_quotes: tuple[str, ...]
    expected_move_label: str
    outcome: str
    outcome_label: str
    process_steps: tuple
    score_source: str = "RULE_BASELINE"
    resolution_status: str = ""


@dataclass(frozen=True)
class BoundaryMapIndicator:
    dimensions: list[BoundaryDimensionIndicator]
    scenario_focus_key: str
    scenario_focus_name: str
    scenario_focus_definition: str
    scenario_focus_score: int | None
    scenario_focus_cue: str
    watchpoint_key: str
    watchpoint_name: str
    watchpoint_definition: str
    watchpoint_score: int | None
    watchpoint_cue: str


def _provider() -> str:
    value = (settings.LLM_PROVIDER or "").strip().lower()
    if value in {"google", "gemini"}:
        return "google"
    if value == "openai":
        return "openai"
    return "openai" if settings.OPENAI_API_KEY else "google"


def _provider_label(provider: str) -> str:
    return "OpenAI" if provider == "openai" else "Gemini"


def _feedback_source_for_provider(provider: str) -> str:
    return (
        Feedback.GenerationSources.OPENAI
        if provider == "openai"
        else Feedback.GenerationSources.GEMINI
    )


def _live_feedback_available(provider: str | None = None) -> bool:
    provider = provider or _provider()
    if provider == "google":
        return bool(settings.GEMINI_API_KEY)
    return bool(settings.OPENAI_API_KEY)


def _openai_client() -> OpenAI:
    return OpenAI(
        api_key=settings.OPENAI_API_KEY,
        timeout=settings.LLM_TIMEOUT_SECONDS,
        max_retries=settings.LLM_MAX_RETRIES,
    )


def _google_client() -> genai.Client:
    return genai.Client(api_key=settings.GEMINI_API_KEY)


def get_prompt_for_type(prompt_type: str, scenario=None) -> Prompt:
    scenario_specific = scenario is not None
    if scenario_specific:
        prompt = Prompt.objects.filter(
            type=prompt_type,
            scenario=scenario,
            is_active=True,
            is_archived=False,
        ).first()
        if prompt:
            return prompt

        global_fallback = Prompt.objects.filter(
            type=prompt_type,
            scenario__isnull=True,
            is_active=True,
            is_archived=False,
        ).first()
        if global_fallback:
            return global_fallback

        label = prompt_type.replace("_", " ").title()
        parts = default_prompt_parts(prompt_type, scenario)
        prompt, _ = Prompt.objects.get_or_create(
            name=f"{scenario.title} {label} Prompt",
            defaults={
                "type": prompt_type,
                "scenario": scenario,
                "context": parts["context"],
                "method": parts["method"],
                "framework_version": Prompt.FRAMEWORK_VERSION,
                "is_active": True,
            },
        )
        fields_to_update = []
        if prompt.type != prompt_type:
            prompt.type = prompt_type
            fields_to_update.append("type")
        if prompt.scenario_id != scenario.id:
            prompt.scenario = scenario
            fields_to_update.append("scenario")
        if not prompt.context:
            prompt.context = parts["context"]
            fields_to_update.append("context")
        if not prompt.method:
            prompt.method = parts["method"]
            fields_to_update.append("method")
        if prompt.framework_version != Prompt.FRAMEWORK_VERSION:
            prompt.framework_version = Prompt.FRAMEWORK_VERSION
            fields_to_update.append("framework_version")
        if prompt.is_archived:
            prompt.is_archived = False
            fields_to_update.append("is_archived")
        if not prompt.is_active:
            prompt.is_active = True
            fields_to_update.append("is_active")
        if fields_to_update:
            prompt.save(update_fields=fields_to_update)
        return prompt

    prompt = Prompt.objects.filter(
        type=prompt_type,
        scenario__isnull=True,
        is_active=True,
        is_archived=False,
    ).first()
    if prompt:
        return prompt

    label = prompt_type.replace("_", " ").title()
    parts = default_prompt_parts(prompt_type)
    prompt, _ = Prompt.objects.get_or_create(
        name=f"Default {label} Prompt",
        defaults={
            "type": prompt_type,
            "context": parts["context"],
            "method": parts["method"],
            "framework_version": Prompt.FRAMEWORK_VERSION,
            "is_active": True,
        },
    )
    fields_to_update = []
    if prompt.type != prompt_type:
        prompt.type = prompt_type
        fields_to_update.append("type")
    if prompt.scenario_id is not None:
        prompt.scenario = None
        fields_to_update.append("scenario")
    if not prompt.context:
        prompt.context = parts["context"]
        fields_to_update.append("context")
    if not prompt.method:
        prompt.method = parts["method"]
        fields_to_update.append("method")
    if prompt.framework_version != Prompt.FRAMEWORK_VERSION:
        prompt.framework_version = Prompt.FRAMEWORK_VERSION
        fields_to_update.append("framework_version")
    if prompt.is_archived:
        prompt.is_archived = False
        fields_to_update.append("is_archived")
    if not prompt.is_active:
        prompt.is_active = True
        fields_to_update.append("is_active")
    if fields_to_update:
        prompt.save(update_fields=fields_to_update)
    return prompt


def _cmcv_editable_layers(prompt: Prompt) -> str:
    return (
        f"CMCV framework: {prompt.framework_version}\n\n"
        "EDITABLE CONTEXT\n"
        f"{prompt.context.strip()}\n\n"
        "EDITABLE METHOD\n"
        f"{prompt.method.strip()}"
    ).strip()


def _roleplay_editable_layers(prompt: Prompt) -> str:
    """Keep editable role-play instructions private from the simulated help-seeker."""
    return (
        "PRIVATE ROLE-PLAY INSTRUCTIONS (use silently; never quote, name, or describe them)\n"
        f"{prompt.context.strip()}\n\n"
        "PRIVATE RESPONSE METHOD (use silently; output only the help-seeker's spoken words)\n"
        f"{prompt.method.strip()}\n\n"
        "Never mention CMCV, any framework or version name, prompts, Context, Method, "
        "contracts, rubrics, scores, system instructions, or hidden evaluation."
    ).strip()


ROLEPLAY_INTERNAL_LEAK_MARKERS = (
    "cmcv",
    "editable context",
    "editable method",
    "private role-play instructions",
    "private response method",
    "protected contract",
    "framework version",
    "system instruction",
    "hidden evaluation",
)

ROLEPLAY_COACHING_LEAK_PATTERNS = (
    re.compile(r"\b(?:please\s+)?(?:do not|don't)\s+(?:just\s+)?tell me what to do\b", re.I),
    re.compile(r"\b(?:please\s+)?(?:do not|don't)\s+(?:try to\s+)?(?:fix|solve)\s+(?:me|this|it)\b", re.I),
    re.compile(r"\b(?:please\s+)?(?:do not|don't)\s+(?:make\s+the\s+decision|decide)\s+for me\b", re.I),
    re.compile(r"\b(?:rather than|instead of)\s+(?:making\s+(?:the|my)\s+decision|deciding)\s+for me\b", re.I),
    re.compile(r"\b(?:i\s+(?:just|only)\s+need|what\s+i\s+need\s+is)\s+(?:you|someone)\s+to\s+(?:just\s+)?listen\b", re.I),
    re.compile(r"\b(?:agency|relationship|safety|integrated)\s+boundary\b", re.I),
    re.compile(r"\b(?:support without fixing|care without overextending|escalate without abandoning)\b", re.I),
)


def _contains_internal_roleplay_leak(text: str) -> bool:
    normalized = " ".join((text or "").lower().split())
    return any(marker in normalized for marker in ROLEPLAY_INTERNAL_LEAK_MARKERS)


def _contains_roleplay_coaching_leak(text: str) -> bool:
    """Reject dialogue that tells the trainee which assessed response to give."""
    compact = " ".join((text or "").split())
    return any(pattern.search(compact) for pattern in ROLEPLAY_COACHING_LEAK_PATTERNS)


def _strip_roleplay_framework_prefix(text: str) -> str:
    """Remove a legacy leading framework label before a help-seeker message is saved."""
    cleaned = (text or "").strip()
    cleaned = re.sub(
        r"(?im)^\s*cmcv(?:\s+framework)?(?:\s*[:：-]?\s*\d+(?:\.\d+)*)?\s*[:：-]?\s*(?:\n+|$)",
        "",
        cleaned,
        count=1,
    )
    return cleaned.strip()


def _placeholder_feedback(session) -> FeedbackPayload:
    boundary_key = session.scenario.boundary_type
    target_key = boundary_framework_for(boundary_key).get("dimension_key", "agency")
    observed_keys = (
        BOUNDARY_DIMENSION_ORDER
        if target_key == "integrated"
        else (target_key,)
    )
    latest_reply = (
        session.messages.filter(sender_type=Message.SenderTypes.USER)
        .order_by("-created_at", "-id")
        .first()
    )
    evidence = latest_reply.content if latest_reply else ""
    return FeedbackPayload(
        boundary_scores=[
            BoundaryScorePayload(
                boundary_key=key.upper(),
                observed=key in observed_keys,
                score=3 if key in observed_keys else None,
                evidence=evidence if key in observed_keys else "",
                rationale=(
                    "Backup Score 3: the response shows partial boundary awareness, "
                    "but the live assessment model was unavailable."
                    if key in observed_keys
                    else "This boundary was not sufficiently elicited in the session."
                ),
                next_move=(
                    "State the relevant boundary explicitly and offer one collaborative next step."
                    if key in observed_keys
                    else ""
                ),
            )
            for key in BOUNDARY_DIMENSION_ORDER
        ],
        what_worked=PLACEHOLDER_STRENGTHS[boundary_key][:2],
        next_boundary_moves=PLACEHOLDER_IMPROVEMENTS[boundary_key][:2],
        review_items=_placeholder_review_items(session),
    )


def _placeholder_review_items(session) -> list[FeedbackReviewItemPayload]:
    user_messages = list(session.messages.filter(sender_type=Message.SenderTypes.USER))
    boundary_type = session.scenario.boundary_type
    helpful_variants = AUTO_TRAINEE_REPLIES.get(boundary_type, {}).get("helpful") or AUTO_TRAINEE_REPLY_FALLBACKS["helpful"]
    issue_map = {
        Scenario.BoundaryTypes.AGENCY: (
            "AGENCY",
            "Choice could remain clearer",
            "The reply could do more to preserve the help-seeker's decision-making.",
        ),
        Scenario.BoundaryTypes.INTEGRATED: (
            "RELATIONSHIP",
            "Integrated boundary could be clearer",
            "The reply could name a sustainable role limit while preserving choice and safety.",
        ),
        Scenario.BoundaryTypes.SAFETY: (
            "SAFETY",
            "Escalation support could be more specific",
            "The reply could make the next support step feel more concrete and less abstract.",
        ),
        Scenario.BoundaryTypes.RELATIONSHIP: (
            "RELATIONSHIP",
            "Relationship limit could be warmer and more specific",
            "The reply could protect the trainee's limits while offering one realistic support option.",
        ),
    }
    item_boundary_key, issue_label, why_it_matters = issue_map.get(
        boundary_type,
        (
            "AGENCY",
            "Response could be stronger",
            "A more targeted peer-support reply would help the conversation move forward more safely.",
        ),
    )

    review_items = []
    for index, message in enumerate(user_messages[:2], start=1):
        review_items.append(
            FeedbackReviewItemPayload(
                trainee_turn_number=index,
                boundary_key=item_boundary_key,
                issue_label=issue_label,
                why_it_matters=why_it_matters,
                better_reply=helpful_variants[(index - 1) % len(helpful_variants)],
            )
        )
    return review_items


def _save_feedback_review_items(
    feedback,
    session,
    review_items: list[FeedbackReviewItemPayload],
    *,
    source: str,
):
    user_messages = list(session.messages.filter(sender_type=Message.SenderTypes.USER))
    allowed_boundary_keys = {
        FeedbackReviewItem.BoundaryKeys.AGENCY,
        FeedbackReviewItem.BoundaryKeys.RELATIONSHIP,
        FeedbackReviewItem.BoundaryKeys.SAFETY,
    }
    scenario_dimension = boundary_framework_for(
        session.scenario.boundary_type
    ).get("dimension_key", "agency").upper()
    if scenario_dimension not in allowed_boundary_keys:
        scenario_dimension = FeedbackReviewItem.BoundaryKeys.AGENCY
    for item in review_items[:3]:
        target_message = None
        if 1 <= item.trainee_turn_number <= len(user_messages):
            target_message = user_messages[item.trainee_turn_number - 1]
        FeedbackReviewItem.objects.create(
            feedback=feedback,
            message=target_message,
            turn_index=item.trainee_turn_number,
            message_excerpt=target_message.content if target_message else "",
            issue_label=item.issue_label,
            why_it_matters=item.why_it_matters,
            better_reply=item.better_reply,
            boundary_key=(
                item.boundary_key.strip().upper()
                if item.boundary_key.strip().upper() in allowed_boundary_keys
                else scenario_dimension
            ),
            source=source,
        )


def _conversation_lines(session) -> list[str]:
    speaker_map = {
        Message.SenderTypes.USER: "Trainee",
        Message.SenderTypes.AI: "AI Help-seeker",
    }
    return [
        f"{speaker_map.get(message.sender_type, message.sender_type)}: {message.content}"
        for message in session.messages.all()
    ]


def _feedback_transcript(session) -> str:
    lines = []
    trainee_turn = 0
    help_seeker_turn = 0

    for session_turn, message in enumerate(session.messages.all(), start=1):
        if message.sender_type == Message.SenderTypes.USER:
            trainee_turn += 1
            lines.append(
                f"Session turn {session_turn} | Trainee turn {trainee_turn}: {message.content}"
            )
        else:
            help_seeker_turn += 1
            lines.append(
                f"Session turn {session_turn} | AI Help-seeker turn {help_seeker_turn}: {message.content}"
            )
    return "\n".join(lines)


def _scenario_prompt_context(session) -> str:
    boundary = boundary_framework_for(session.scenario.boundary_type)
    targeted_context = scenario_prompt_context_for(
        Prompt.PromptTypes.ROLE_PLAY,
        session.scenario.boundary_type,
    )
    return (
        f"Boundary construct: {boundary.get('canonical_name', session.scenario.title)}\n"
        f"Practice context: {boundary.get('practice_context', session.scenario.title)}\n"
        f"Live dimension: {boundary.get('dimension_key', 'agency')}\n"
        f"Operational focus: {boundary.get('prompt_context', '')}\n"
        "Version-controlled scenario elicitation context:\n"
        f"{targeted_context}"
    )


def _finish_reason(response: Any) -> str:
    candidates = getattr(response, "candidates", None) or []
    if not candidates:
        return ""
    reason = getattr(candidates[0], "finish_reason", "") or ""
    return str(reason)


def _normalized_text(text: str) -> str:
    return f" {(text or '').strip().lower()} "


def _marker_score(text: str, markers: list[str]) -> int:
    normalized = _normalized_text(text)
    return sum(1 for marker in markers if marker in normalized)


def _user_turn_count(session) -> int:
    return session.messages.filter(sender_type=Message.SenderTypes.USER).count()


def _latest_user_message(session):
    return (
        session.messages.filter(sender_type=Message.SenderTypes.USER)
        .order_by("-created_at", "-id")
        .first()
    )


def _latest_ai_message(session):
    return (
        session.messages.filter(sender_type=Message.SenderTypes.AI)
        .order_by("-created_at", "-id")
        .first()
    )


def _conversation_exchanges(session) -> list[tuple[str, str]]:
    """Pair each trainee reply with the AI message that actually elicited it."""

    exchanges = []
    preceding_ai_text = ""
    for message in session.messages.order_by("created_at", "id"):
        if message.sender_type == Message.SenderTypes.AI:
            preceding_ai_text = message.content
        elif message.sender_type == Message.SenderTypes.USER:
            exchanges.append((preceding_ai_text, message.content))
    return exchanges


def deterministic_feedback_assessments(session):
    """Return the protected session-score assessment for each boundary."""

    target_key = boundary_framework_for(session.scenario.boundary_type)["dimension_key"]
    exchanges = _conversation_exchanges(session)
    return {
        key: evaluate_boundary_session(key, target_key, exchanges)
        for key in BOUNDARY_DIMENSION_ORDER
    }


def _final_feedback_judge_run(session):
    return latest_resolved_geval_run(
        session,
        purposes=[BoundaryEvaluationRun.Purposes.FINAL],
    )


def _accepted_judge_assessment(run, key):
    detail = run.resolution_details.get(key.upper(), {})
    if detail.get("decision") not in JUDGE_ACCEPTED_DECISIONS:
        return None

    assessment = aggregate_persisted_geval_boundary(
        key,
        run.turn_evaluations.all(),
    )
    protected_score = run.resolved_scores.get(key.upper())
    if not assessment.observed or protected_score is None:
        return None
    return replace(assessment, score=protected_score)


def _trusted_live_feedback_assessments(session):
    """Keep accepted semantic evidence when a later FINAL audit is unavailable."""

    runs = (
        session.boundary_evaluation_runs.filter(
            status=BoundaryEvaluationRun.Statuses.SUCCEEDED,
            purpose=BoundaryEvaluationRun.Purposes.LIVE,
            evaluator_version=GEVAL_VERSION,
            prompt_version=GEVAL_PROMPT_VERSION,
            rubric_version=EVIDENCE_RUBRIC_VERSION,
            model=getattr(
                settings,
                "BOUNDARY_JUDGE_MODEL",
                settings.OPENAI_MODEL,
            ),
        )
        .prefetch_related("turn_evaluations")
        .order_by("-evaluated_through_message_id", "-created_at", "-id")
    )
    resolved = {}
    for run in runs:
        for key in BOUNDARY_DIMENSION_ORDER:
            if key in resolved:
                continue
            assessment = _accepted_judge_assessment(run, key)
            if assessment is not None:
                resolved[key] = assessment
        if len(resolved) == len(BOUNDARY_DIMENSION_ORDER):
            break
    return resolved


def final_feedback_assessments(session):
    """Return session-wide assessments, preferring an accepted FINAL audit."""

    assessments = deterministic_feedback_assessments(session)
    judge_run = _final_feedback_judge_run(session)
    resolved = dict(assessments)
    if judge_run is not None:
        for key in BOUNDARY_DIMENSION_ORDER:
            judge_assessment = _accepted_judge_assessment(judge_run, key)
            if judge_assessment is not None:
                resolved[key] = judge_assessment

    # A successful LIVE run may contain trusted evidence even when the final audit
    # times out. It can fill an unobserved gap, but never overwrite a full-session
    # deterministic or accepted FINAL assessment.
    for key, live_assessment in _trusted_live_feedback_assessments(session).items():
        if not resolved[key].observed:
            resolved[key] = live_assessment
    return resolved


def _session_evidence_text(assessment) -> str:
    """Format the full chronological evidence ledger for persisted feedback."""

    lines = []
    seen = set()
    for event in assessment.events:
        for quote in event.evidence_quotes:
            cleaned = quote.strip()
            marker = (event.turn_number, cleaned)
            if not cleaned or marker in seen:
                continue
            seen.add(marker)
            lines.append(
                f'Turn {event.turn_number} | {event.move_label} | '
                f'{event.outcome_label}: "{cleaned}"'
            )
    return "\n".join(lines)


def _clamp(value: int, lower: int, upper: int) -> int:
    return max(lower, min(upper, value))


def _scaled_points(score: int, weight: int) -> float:
    bounded_score = max(0, min(score, 4))
    return (bounded_score / 4) * weight


def _clean_generated_text(text: str) -> str:
    cleaned = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    cleaned = cleaned.replace("\ufeff", "").replace("\u200b", "").replace("\xa0", " ")
    cleaned = cleaned.strip()
    if not cleaned:
        return ""

    prefixes = [
        "AI Help-seeker:",
        "AI help-seeker:",
        "Help-seeker:",
        "Help seeker:",
        "AI:",
        "Assistant:",
        "Role-play reply:",
        "Reply:",
    ]

    changed = True
    while cleaned and changed:
        changed = False
        stripped_lines = [line.strip() for line in cleaned.split("\n")]
        while stripped_lines and not stripped_lines[0]:
            stripped_lines.pop(0)
            changed = True
        if stripped_lines:
            first_line = stripped_lines[0]
            for prefix in prefixes:
                if first_line.startswith(prefix):
                    first_line = first_line[len(prefix):].strip()
                    stripped_lines[0] = first_line
                    changed = True
            if first_line in {"AI Help-seeker", "AI help-seeker", "Help-seeker", "Help seeker", "AI", "Assistant"}:
                stripped_lines.pop(0)
                changed = True
        cleaned = "\n".join(stripped_lines).strip()

    while "\n\n\n" in cleaned:
        cleaned = cleaned.replace("\n\n\n", "\n\n")

    if cleaned.startswith(("'", '"')) and cleaned.endswith(("'", '"')) and len(cleaned) > 2:
        cleaned = cleaned[1:-1].strip()

    return cleaned


def _looks_incomplete_roleplay_reply(text: str) -> bool:
    stripped = (text or "").strip()
    if not stripped:
        return True
    if not any(character.isalpha() for character in stripped):
        return True
    if stripped.endswith(("'", '"', "(", "[", "{", "/", "\\", ":", ";", ",")):
        return True
    if stripped.count('"') % 2 == 1:
        return True
    if stripped.count("(") > stripped.count(")"):
        return True
    if stripped.endswith((" don", " can", " won", " isn", " aren", " didn", " shouldn", " couldn")):
        return True
    if len(stripped) < 48 and stripped[-1].isalnum() and not stripped.endswith((".", "!", "?")):
        return True
    words = stripped.split()
    if words and len(words[-1]) <= 3 and stripped[-1].isalnum() and not stripped.endswith((".", "!", "?")):
        return True
    return False


def _recent_ai_signatures(session, limit=3) -> list[str]:
    messages = list(
        session.messages.filter(sender_type=Message.SenderTypes.AI).values_list("content", flat=True)
    )[-limit:]
    signatures = []
    for text in messages:
        compact = " ".join((text or "").split())
        if compact:
            signatures.append(compact[:110])
    return signatures


def _recent_user_signatures(session, limit=3) -> list[str]:
    messages = list(
        session.messages.filter(sender_type=Message.SenderTypes.USER).values_list("content", flat=True)
    )[-limit:]
    signatures = []
    for text in messages:
        compact = " ".join((text or "").split())
        if compact:
            signatures.append(compact[:110])
    return signatures


def _repetition_ratio(left: str, right: str) -> float:
    normalized_left = " ".join((left or "").strip().lower().split())
    normalized_right = " ".join((right or "").strip().lower().split())
    if not normalized_left or not normalized_right:
        return 0.0
    return SequenceMatcher(None, normalized_left, normalized_right).ratio()


def _is_repetitive_roleplay_reply(session, text: str) -> bool:
    candidate = (text or "").strip()
    if not candidate:
        return False
    for previous in _recent_ai_signatures(session, limit=2):
        if candidate == previous:
            return True
        if _repetition_ratio(candidate, previous) >= 0.78:
            return True
    return False


def _is_repetitive_trainee_reply(session, text: str) -> bool:
    candidate = (text or "").strip()
    if not candidate:
        return False
    for previous in _recent_user_signatures(session, limit=2):
        if candidate == previous:
            return True
        if _repetition_ratio(candidate, previous) >= 0.78:
            return True
    return False


def _roleplay_quality_issue(session, text: str, user_message: str = "") -> str:
    if not (text or "").strip():
        return "empty"
    if _contains_internal_roleplay_leak(text):
        return "internal_instruction_leak"
    if _contains_roleplay_coaching_leak(text):
        return "coaching_leak"
    if _looks_incomplete_roleplay_reply(text):
        return "incomplete"
    if _is_repetitive_roleplay_reply(session, text):
        return "repetitive"
    if (
        user_message
        and session.scenario.boundary_type == Scenario.BoundaryTypes.RELATIONSHIP
    ):
        relationship = classify_relationship_response(user_message)
        normalised_reply = _normalized_text(text)
        protects_supporter_markers = (
            "so i do not rely on you",
            "so i don't rely on you",
            "too much responsibility for one person",
            "help me find someone else",
            "help me find another person",
            "backup contact",
            "backup person",
            "other person i can contact",
            "someone else i can contact",
        )
        if relationship.state in {"OVERCOMMITMENT", "MIXED_BOUNDARY"} and any(
            marker in normalised_reply for marker in protects_supporter_markers
        ):
            return "relationship_branch_mismatch"
    return ""


def _demo_trainee_reply_quality_issue(session, text: str) -> str:
    if not (text or "").strip():
        return "empty"
    if _looks_incomplete_roleplay_reply(text):
        return "incomplete"
    if _is_repetitive_trainee_reply(session, text):
        return "repetitive"
    return ""


def _repair_instruction_for_issue(issue: str) -> str:
    if issue == "internal_instruction_leak":
        return (
            "Your previous draft exposed private instructions or framework terminology. "
            "Write only a natural first-person help-seeker message. Do not mention CMCV, "
            "frameworks, prompts, Context, Method, contracts, rubrics, scores, or instructions."
        )
    if issue == "coaching_leak":
        return (
            "Your previous draft told the trainee which support technique or assessed response to use. "
            "Rewrite it as natural first-person help-seeker dialogue. Express the situation, emotion, "
            "uncertainty, or request without telling the trainee to avoid advice, fixing, deciding, "
            "overcommitting, or escalation. Do not name any boundary or training objective."
        )
    if issue == "incomplete":
        return (
            "Your previous draft was cut off, too short, or not natural-language dialogue. "
            "Write one complete alternative reply with a clear ending. Never output a score, "
            "score, bare number, rubric label, or metadata."
        )
    if issue == "repetitive":
        return (
            "Your previous draft repeated a recent help-seeker move or phrasing. Write a fresher "
            "next turn that reacts to the trainee's latest wording, advances the scenario by one "
            "human step, and avoids reusing the same request, opening phrase, or sentence shape."
        )
    if issue == "relationship_branch_mismatch":
        return (
            "The trainee has not yet set a clear sustainable relationship boundary. Do not coach them, "
            "protect their boundary for them, suggest backup support, or volunteer to reduce dependency. "
            "React only as the help-seeker: increase dependency pressure by one believable step, or ask "
            "what their vague availability promise actually means."
        )
    return (
        "Write one stronger alternative reply that sounds more like a real help-seeker and less "
        "like a generic template."
    )


def _repair_instruction_for_demo_trainee_reply(issue: str, quality: str) -> str:
    if issue == "incomplete":
        return (
            "Your previous draft was incomplete. Rewrite it as one complete trainee reply with a clear ending."
        )
    if issue == "repetitive":
        return (
            "Your previous draft repeated the trainee's recent wording too closely. Write a fresher trainee reply "
            "that reacts to the latest help-seeker turn and changes the sentence shape."
        )
    if quality == "unhelpful":
        return (
            "Write one clearly weaker Band 1 or Band 2 trainee reply. It should still sound human, but it should "
            "show a meaningful empathy, boundary, or referral mistake rather than landing in a mixed middle band."
        )
    return (
        "Write one clearly stronger Band 4 or Band 5 trainee peer-support reply that sounds natural, warm, and "
        "specific without sounding scripted."
    )


def _trainee_quality_brief(boundary_type: str, quality: str) -> str:
    helpful_map = {
        Scenario.BoundaryTypes.AGENCY: (
            "Lead with validation or reflection, show you understand what the help-seeker is carrying, and ask one gentle open question instead of jumping straight to fixing."
        ),
        Scenario.BoundaryTypes.INTEGRATED: (
            "Stay supportive, reflect the worry underneath the disclosure, make it clear names are not needed, and guide the conversation toward a safe next step."
        ),
        Scenario.BoundaryTypes.SAFETY: (
            "Acknowledge the emotional weight, explain that extra support is warranted, and name one concrete, collaborative next step."
        ),
        Scenario.BoundaryTypes.RELATIONSHIP: (
            "Validate the need, state a kind but explicit limit, and offer one realistic alternative source of support."
        ),
    }
    unhelpful_map = {
        Scenario.BoundaryTypes.AGENCY: (
            "Make 1 or 2 believable mistakes such as over-reassuring too early, giving advice too fast, or staying too surface-level."
        ),
        Scenario.BoundaryTypes.INTEGRATED: (
            "Make 1 or 2 believable mistakes such as inviting more identifying details, over-promising confidentiality, or being vague about limits."
        ),
        Scenario.BoundaryTypes.SAFETY: (
            "Make 1 or 2 believable mistakes such as sounding too generic, too abrupt about referral, or minimising how hard it feels."
        ),
        Scenario.BoundaryTypes.RELATIONSHIP: (
            "Make 1 or 2 believable mistakes such as over-promising availability, leaving boundaries unclear, or offering unsustainable support."
        ),
    }
    if quality == "unhelpful":
        return unhelpful_map.get(
            boundary_type,
            "Make 1 or 2 believable peer-support mistakes, but keep the reply plausible and human.",
        )
    return helpful_map.get(
        boundary_type,
        "Write a warm, bounded, non-clinical trainee reply that helps the conversation move forward safely.",
    )


def _trainee_quality_band_target(boundary_type: str, quality: str) -> str:
    helpful_map = {
        Scenario.BoundaryTypes.AGENCY: (
            "Aim for Band 4 to Band 5 on the Relational Focus Boundary: strong empathy, reflective listening, and one gentle open question."
        ),
        Scenario.BoundaryTypes.INTEGRATED: (
            "Aim for Band 4 to Band 5 on the Privacy & Confidentiality Boundary: protect privacy clearly, avoid names/details, and stay focused on the concern."
        ),
        Scenario.BoundaryTypes.SAFETY: (
            "Aim for Band 4 to Band 5 on the Role Scope & Escalation Boundary: acknowledge seriousness, stay emotionally present, and offer one concrete collaborative support step."
        ),
        Scenario.BoundaryTypes.RELATIONSHIP: (
            "Aim for Band 4 to Band 5 on the Time & Availability Boundary: hold a kind but explicit availability limit and add one realistic alternative."
        ),
    }
    unhelpful_map = {
        Scenario.BoundaryTypes.AGENCY: (
            "Aim for Band 1 to Band 2 on the Relational Focus Boundary: minimise, reassure too fast, or advise before enough understanding."
        ),
        Scenario.BoundaryTypes.INTEGRATED: (
            "Aim for Band 1 to Band 2 on the Privacy & Confidentiality Boundary: invite identifying details, imply secrecy, or leave privacy limits vague."
        ),
        Scenario.BoundaryTypes.SAFETY: (
            "Aim for Band 1 to Band 2 on the Role Scope & Escalation Boundary: sound abrupt, generic, emotionally thin, or weakly collaborative about extra support."
        ),
        Scenario.BoundaryTypes.RELATIONSHIP: (
            "Aim for Band 1 to Band 2 on the Time & Availability Boundary: over-promise availability, accept becoming the main support, or withdraw coldly without a collaborative alternative."
        ),
    }
    mapping = unhelpful_map if quality == "unhelpful" else helpful_map
    default = (
        "Aim for Band 1 to Band 2 with a meaningful but plausible helping error."
        if quality == "unhelpful"
        else "Aim for Band 4 to Band 5 with warm, boundaried, specific peer support."
    )
    return mapping.get(boundary_type, default)


def _trainee_quick_reply_system_prompt(session, quality: str) -> str:
    scenario_brief = scenario_roleplay_brief(session.scenario.boundary_type)
    transcript = "\n".join(_conversation_lines(session))
    recent_trainee_lines = "\n".join(f"- {line}" for line in _recent_user_signatures(session)) or "- None yet."
    quality_instruction = (
        "Write one clearly stronger trainee peer-support reply."
        if quality == "helpful"
        else "Write one clearly weaker trainee reply for testing."
    )

    return (
        f"{quality_instruction}\n\n"
        "You are writing the TRAINEE'S next message in a peer-support training conversation.\n\n"
        f"{_scenario_prompt_context(session)}\n"
        f"Learning objectives:\n{session.scenario.learning_objectives}\n\n"
        "Scenario behaviour brief:\n"
        f"- Core need: {scenario_brief.get('core_need', 'Offer peer support within scope.')}\n"
        f"- Pressure focus: {scenario_brief.get('pressure_focus', 'Notice what the conversation is testing.')}\n"
        f"- If trainee is supportive: {scenario_brief.get('supportive_shift', 'The help-seeker should feel more able to continue.')}\n"
        f"- If trainee is unhelpful: {scenario_brief.get('unhelpful_shift', 'The help-seeker may stay guarded or unsure.')}\n\n"
        "Conversation so far:\n"
        f"{transcript}\n\n"
        "Recent trainee wording to avoid copying too closely:\n"
        f"{recent_trainee_lines}\n\n"
        "Quality target for this reply:\n"
        f"- {_trainee_quality_brief(session.scenario.boundary_type, quality)}\n"
        f"- {_trainee_quality_band_target(session.scenario.boundary_type, quality)}\n"
        "- A stronger reply should usually show emotional attunement, a brief sign of understanding, scope-safe wording, and a collaborative next step that fits the scenario.\n"
        "- A weaker reply should still sound plausible and human, but it should miss at least one high-value skill or allow one meaningful boundary drift.\n"
        "- Do not produce a middling Band 3 answer. The contrast between helpful and unhelpful mode should be obvious in empathy, boundary handling, and next-step support.\n"
        "- Silently check empathy, boundary management, referral/scope handling, and specificity before writing the final reply.\n"
        "- The reply must sound like a real student peer supporter, not a therapist, rubric, or system message.\n"
        "- Speak directly to the help-seeker.\n"
        "- Do not write stage directions, analysis, labels, bullet points, or role names.\n"
        "- Keep it to 1 to 3 sentences.\n"
        f"- Write in {DEFAULT_RESPONSE_LANGUAGE}.\n"
        "- If this is a weaker reply, it should still be plausible and human rather than exaggerated or cruel."
    )


def _openai_demo_trainee_reply(session, quality: str) -> str:
    client = _openai_client()
    system_prompt = _trainee_quick_reply_system_prompt(session, quality)
    repair_instruction = ""
    trainee_text = ""
    last_issue = ""

    for attempt in range(DEMO_TRAINEE_REPAIR_ATTEMPTS):
        input_payload = [{"role": "developer", "content": system_prompt}]
        if repair_instruction:
            input_payload.append({"role": "developer", "content": repair_instruction})
        input_payload.append(
            {
                "role": "user",
                "content": "Write only the trainee's next message.",
            }
        )

        # This is where the OpenAI API call happens.
        response = client.responses.create(
            model=settings.OPENAI_MODEL,
            reasoning={"effort": "low"},
            max_output_tokens=DEMO_TRAINEE_MAX_OUTPUT_TOKENS,
            input=input_payload,
        )
        trainee_text = _clean_generated_text(response.output_text or "")
        last_issue = _demo_trainee_reply_quality_issue(session, trainee_text)
        if not last_issue:
            return trainee_text
        logger.warning(
            "Retrying OpenAI trainee demo reply for session %s due to %s on attempt %s.",
            session.id,
            last_issue,
            attempt + 1,
        )
        repair_instruction = _repair_instruction_for_demo_trainee_reply(last_issue, quality)

    if trainee_text and last_issue != "empty":
        return trainee_text
    raise ValueError("Model returned an empty trainee demo reply.")


def _google_demo_trainee_reply(session, quality: str) -> str:
    client = _google_client()
    system_prompt = _trainee_quick_reply_system_prompt(session, quality)
    user_prompt = "Write only the trainee's next message."

    # This is where the Google Gemini API call happens.
    response = client.models.generate_content(
        model=settings.GOOGLE_MODEL,
        contents=user_prompt,
        config=google_types.GenerateContentConfig(
            system_instruction=system_prompt,
            temperature=0.7 if quality == "helpful" else 0.8,
            max_output_tokens=DEMO_TRAINEE_MAX_OUTPUT_TOKENS,
            thinking_config=google_types.ThinkingConfig(thinking_budget=0),
        ),
    )
    trainee_text = _clean_generated_text(response.text or "")
    finish_reason = _finish_reason(response)
    quality_issue = _demo_trainee_reply_quality_issue(session, trainee_text)
    if finish_reason.endswith("MAX_TOKENS") or quality_issue:
        repair_instruction = _repair_instruction_for_demo_trainee_reply(
            quality_issue or "incomplete",
            quality,
        )
        retry_response = client.models.generate_content(
            model=settings.GOOGLE_MODEL,
            contents=user_prompt,
            config=google_types.GenerateContentConfig(
                system_instruction=f"{system_prompt}\n\n{repair_instruction}",
                temperature=0.6 if quality == "helpful" else 0.75,
                max_output_tokens=DEMO_TRAINEE_MAX_OUTPUT_TOKENS + 60,
                thinking_config=google_types.ThinkingConfig(thinking_budget=0),
            ),
        )
        retry_text = _clean_generated_text(retry_response.text or "")
        retry_issue = _demo_trainee_reply_quality_issue(session, retry_text)
        if retry_text and not retry_issue:
            trainee_text = retry_text
        elif retry_text:
            trainee_text = retry_text
    if not trainee_text:
        raise ValueError("Gemini returned an empty trainee demo reply.")
    return trainee_text


def _template_demo_trainee_reply(session, quality: str) -> str:
    current_turns = session.messages.filter(sender_type=Message.SenderTypes.USER).count()
    return auto_trainee_reply_for(
        session.scenario.boundary_type,
        quality,
        turn_index=current_turns,
    )


def _demo_trainee_fallback_notice_text(reason: str, provider: str, exc: Exception) -> str:
    provider_label = _provider_label(provider)
    if reason == "quota_exhausted":
        return (
            f"{provider_label} could not generate the quick test reply because of quota, billing, or rate limits, "
            "so a scenario template was used instead."
        )
    if reason == "configuration":
        return (
            f"{provider_label} is not configured correctly for quick test replies right now, "
            "so a scenario template was used instead."
        )
    if reason == "network":
        return (
            f"{provider_label} could not be reached from the current network for the quick test reply, "
            "so a scenario template was used instead."
        )
    return (
        f"{provider_label} failed to generate the quick test reply, so a scenario template was used instead."
    )


def _boundary_status_summary(boundary_type: str, score: int | None) -> str:
    if score is None:
        return (
            "Send one trainee reply and PeerTrain will estimate how steady your current boundary sounds."
        )

    if boundary_type == Scenario.BoundaryTypes.INTEGRATED:
        summaries = {
            5: "You are containing identifying details while still sounding calm and supportive.",
            4: "You are mostly protecting privacy, with only a little room to make the limit clearer.",
            3: "Your tone is supportive, but the privacy boundary is still not fully explicit.",
            2: "Your wording may be letting the conversation drift toward unsafe third-party details.",
            1: "There is a strong risk that the reply invites gossip, oversharing, or unrealistic secrecy promises.",
        }
    elif boundary_type == Scenario.BoundaryTypes.SAFETY:
        summaries = {
            5: "You are pairing warmth with a concrete, compassionate next-step support option.",
            4: "The referral direction is mostly good, but it could feel a little more specific or emotionally held.",
            3: "You are trying to help, but the support step still sounds vague, uneven, or only partly warm.",
            2: "The reply may feel too abrupt, too detached, or too unclear about what support comes next.",
            1: "The help-seeker may feel brushed off, over-directed, or left alone with a cold referral.",
        }
    elif boundary_type == Scenario.BoundaryTypes.RELATIONSHIP:
        summaries = {
            5: "You are sounding caring while also naming realistic limits and a sustainable alternative.",
            4: "Your boundary is mostly holding, though it could be a little clearer or more concrete.",
            3: "You still sound supportive, but your limit-setting is mixed or slightly vague.",
            2: "The boundary is starting to slip, or the alternative support route is not clear enough yet.",
            1: "The reply is likely over-promising time, availability, or emotional responsibility.",
        }
    else:
        summaries = {
            5: "You are staying empathic and curious without rushing to fix or tidy away the distress.",
            4: "The response is mostly containing, with just a little room to deepen or slow down.",
            3: "Your warmth is visible, but the response still feels a bit generic, rushed, or surface-level.",
            2: "The reply may be moving too quickly into fixing, reassurance, or low-empathy guidance.",
            1: "The help-seeker is unlikely to feel well contained by this wording right now.",
        }
    return summaries[score]


def _boundary_status_cue(boundary_type: str, score: int | None) -> str:
    if score is None:
        return "This live cue is only a guide. After the first trainee turn, it will react to your wording."

    if boundary_type == Scenario.BoundaryTypes.INTEGRATED:
        if score >= 4:
            return "Keep redirecting toward the person's worry, uncertainty, and next safe step rather than names or specifics."
        if score == 3:
            return "Try one clearer phrase such as 'You do not need to name them for us to think this through safely.'"
        return "Next reply: gently stop the details, avoid absolute secrecy promises, and focus on the dilemma underneath the disclosure."

    if boundary_type == Scenario.BoundaryTypes.SAFETY:
        if score >= 4:
            return "Keep the warmth, then make the next support option concrete enough that it feels usable, not abstract."
        if score == 3:
            return "Try pairing care with one specific option, such as a helpline, GP, counselling service, or trusted person."
        return "Next reply: avoid sounding like you are handing the person off. Stay present and name one realistic support step."

    if boundary_type == Scenario.BoundaryTypes.RELATIONSHIP:
        if score >= 4:
            return "Keep naming your limits with warmth, then offer one realistic person, service, or short check-in alternative."
        if score == 3:
            return "Try making the limit more explicit and pair it with one concrete alternative instead of a broad promise."
        return "Next reply: do not promise ongoing availability. Name one clear limit and one safer support option."

    if score >= 4:
        return "Keep leading with validation and gentle curiosity before shifting to suggestions or next steps."
    if score == 3:
        return "Try reflecting the feeling more directly and adding one open question before moving on."
    return "Next reply: slow down, reduce fixing language, and show that you understand the emotional weight first."


def _boundary_score_meta(score: int | None) -> tuple[str, str]:
    if score is None:
        return "neutral", "Waiting"

    tone_map = {
        5: "steady",
        4: "good",
        3: "mixed",
        2: "wobbling",
        1: "risk",
    }
    label_map = {
        5: "Very steady",
        4: "Mostly steady",
        3: "Mixed",
        2: "Wobbling",
        1: "At risk",
    }
    return tone_map[score], label_map[score]


def _boundary_dimension_definition(key: str) -> str:
    return boundary_framework_for_dimension(key)["definition"]


def _boundary_dimension_name(key: str) -> str:
    return boundary_framework_for_dimension(key)["canonical_name"]


def _boundary_dimension_cue(dimension_key: str, score: int | None) -> str:
    if score is None:
        return "After your first trainee turn, PeerTrain will show which boundary line needs the most attention."

    if dimension_key == "relationship":
        if score >= 4:
            return "Keep the warmth, hold the limit, and keep moving toward one realistic alternative instead of broader reassurance."
        if score == 3:
            return "Try making the limit more explicit and pair it with one concrete person, service, or short check-in option."
        return "Next reply: avoid unlimited access. Say what you can do, what you cannot do, and one safer alternative."

    if dimension_key == "agency":
        if score >= 4:
            return "Keep the spotlight on the help-seeker. Validation plus one gentle, specific question is enough."
        if score == 3:
            return "Reduce fixing and keep your own story in the background unless it clearly serves the help-seeker."
        return "Next reply: validate first, ask one grounded question, and avoid shifting into advice or your own experience."

    if score >= 4:
        return "Keep the warmth and make the next support step concrete enough that it feels usable."
    if score == 3:
        return "Try naming one specific support or safety step while staying emotionally present."
    return "Next reply: if risk or complexity is rising, stay warm and move toward safety checking or extra support."


def _judge_process_steps(run, boundary_key):
    rubric = BOUNDARY_RUBRICS[boundary_key]
    items = [
        item
        for item in run.turn_evaluations.all()
        if item.boundary_key == boundary_key.upper()
        and item.applicable
        and item.expected_move_key
    ]
    status_map = {}
    for move_key, _ in rubric.process_moves:
        move_items = [
            item for item in items if item.expected_move_key == move_key
        ]
        if not move_items:
            status_map[move_key] = "not_reached"
        elif any(item.critical_concern for item in move_items):
            status_map[move_key] = "concern"
        else:
            latest = max(
                move_items,
                key=lambda item: (item.trainee_turn_number, item.id),
            )
            status_map[move_key] = {
                "MET": "met",
                "PARTLY_MET": "partly_met",
                "MISSED": "missed",
                "BOUNDARY_CONCERN": "concern",
            }.get(latest.judgement, "not_reached")
    return tuple(
        BoundaryProcessStepResult(move_key, label, status_map[move_key])
        for move_key, label in rubric.process_moves
    )


def _judge_backed_dimension(run, boundary_key, fallback_dimension):
    detail = run.resolution_details.get(boundary_key.upper(), {})
    if detail.get("decision") not in JUDGE_ACCEPTED_DECISIONS:
        return replace(
            fallback_dimension,
            score_source="RULE_FALLBACK",
            resolution_status=detail.get("decision", ""),
        )

    items = [
        item
        for item in run.turn_evaluations.all()
        if item.boundary_key == boundary_key.upper() and item.applicable
    ]
    if not items:
        return fallback_dimension
    latest = max(items, key=lambda item: (item.trainee_turn_number, item.id))
    score = run.resolved_scores.get(boundary_key.upper())
    tone, label = _boundary_score_meta(score)
    outcome = {
        "MET": "met",
        "PARTLY_MET": "partly_met",
        "MISSED": "missed",
        "BOUNDARY_CONCERN": "concern",
    }.get(latest.judgement, "not_applicable")
    outcome_label = {
        "met": "Met",
        "partly_met": "Partly met",
        "missed": "Missed",
        "concern": "Boundary concern",
        "not_applicable": "Not applicable",
    }[outcome]
    is_major_concern = (
        latest.judgement == BoundaryTurnEvaluation.Judgements.BOUNDARY_CONCERN
        and not latest.critical_concern
    )
    return BoundaryDimensionIndicator(
        key=boundary_key,
        name=_boundary_dimension_name(boundary_key),
        definition=_boundary_dimension_definition(boundary_key),
        score=score,
        tone=tone,
        label=label,
        summary=latest.rationale,
        observed=True,
        rubric_version=run.rubric_version,
        evidence_met=(latest.expected_move,) if latest.judgement == "MET" else (),
        evidence_missing=(
            (latest.expected_move,) if latest.judgement != "MET" else ()
        ),
        major_concerns=(
            (latest.evidence_interpretation,) if is_major_concern else ()
        ),
        critical_breaches=(
            (latest.evidence_interpretation,) if latest.critical_concern else ()
        ),
        evidence_quotes=tuple(latest.evidence_quotes),
        expected_move_label=latest.expected_move,
        outcome=outcome,
        outcome_label=outcome_label,
        process_steps=_judge_process_steps(run, boundary_key),
        score_source="LLM_JUDGE",
        resolution_status=detail.get("decision", ""),
    )


def build_boundary_map_indicator(session) -> BoundaryMapIndicator:
    scenario_framework = boundary_framework_for(session.scenario.boundary_type)
    scenario_focus_key = scenario_framework["dimension_key"]
    display_keys = (
        list(BOUNDARY_DIMENSION_ORDER)
        if scenario_focus_key == "integrated"
        else [scenario_focus_key]
    )
    latest_user_message = _latest_user_message(session)
    if latest_user_message is None:
        dimensions = []
        for key in display_keys:
            tone, label = _boundary_score_meta(None)
            dimensions.append(
                BoundaryDimensionIndicator(
                    key=key,
                    name=_boundary_dimension_name(key),
                    definition=_boundary_dimension_definition(key),
                    score=None,
                    tone=tone,
                    label=label,
                    summary="Waiting for your first trainee reply.",
                    observed=False,
                    rubric_version=RUBRIC_VERSION,
                    evidence_met=(),
                    evidence_missing=(),
                    major_concerns=(),
                    critical_breaches=(),
                    evidence_quotes=(),
                    expected_move_label="",
                    outcome="not_applicable",
                    outcome_label="Not applicable",
                    process_steps=(),
                )
            )
        return BoundaryMapIndicator(
            dimensions=dimensions,
            scenario_focus_key=scenario_focus_key,
            scenario_focus_name=scenario_framework["canonical_name"],
            scenario_focus_definition=scenario_framework["definition"],
            scenario_focus_score=None,
            scenario_focus_cue=_boundary_dimension_cue(scenario_focus_key, None),
            watchpoint_key="",
            watchpoint_name="",
            watchpoint_definition="",
            watchpoint_score=None,
            watchpoint_cue="",
        )

    exchanges = _conversation_exchanges(session)
    rubric_results = {
        key: evaluate_boundary_conversation(
            key=key,
            target_key=scenario_focus_key,
            exchanges=exchanges,
        )
        for key in display_keys
    }
    judge_run = latest_resolved_geval_run(session)
    dimensions = []
    for key in display_keys:
        rubric_result = rubric_results[key]
        score = rubric_result.score
        tone, label = _boundary_score_meta(score)
        if not rubric_result.observed:
            label = "Not observed"
        dimension = BoundaryDimensionIndicator(
                key=key,
                name=_boundary_dimension_name(key),
                definition=_boundary_dimension_definition(key),
                score=score,
                tone=tone,
                label=label,
                summary=rubric_result.rationale,
                observed=rubric_result.observed,
                rubric_version=RUBRIC_VERSION,
                evidence_met=rubric_result.met_labels,
                evidence_missing=rubric_result.missing_labels,
                major_concerns=rubric_result.major_concerns,
                critical_breaches=rubric_result.critical_breaches,
                evidence_quotes=rubric_result.evidence_quotes,
                expected_move_label=rubric_result.expected_move_label,
                outcome=rubric_result.outcome,
                outcome_label=rubric_result.outcome_label,
                process_steps=rubric_result.process_steps,
        )
        if judge_run is not None:
            dimension = _judge_backed_dimension(judge_run, key, dimension)
        dimensions.append(dimension)

    dimension_order = {key: index for index, key in enumerate(display_keys)}
    observed_dimensions = [item for item in dimensions if item.observed]
    watchpoint = (
        min(
            observed_dimensions,
            key=lambda item: (
                item.score if item.score is not None else 99,
                dimension_order.get(item.key, 99),
            ),
        )
        if scenario_focus_key == "integrated" and observed_dimensions
        else None
    )
    if scenario_focus_key == "integrated":
        active = next(
            (item for item in dimensions if item.observed and item.score is not None),
            None,
        )
        focus_score = active.score if active else None
        focus_name = scenario_framework["canonical_name"]
        focus_definition = scenario_framework["definition"]
        focus_cue = (
            (
                f"Current turn: {active.name}. {active.expected_move_label}."
                if active and active.expected_move_label
                else "PeerTrain will identify the boundary made relevant by the current turn."
            )
        )
    else:
        scenario_focus = next(
            item for item in dimensions if item.key == scenario_focus_key
        )
        focus_score = scenario_focus.score
        focus_name = scenario_focus.name
        focus_definition = scenario_focus.definition
        focus_cue = _boundary_dimension_cue(
            scenario_focus.key,
            scenario_focus.score,
        )

    return BoundaryMapIndicator(
        dimensions=dimensions,
        scenario_focus_key=scenario_focus_key,
        scenario_focus_name=focus_name,
        scenario_focus_definition=focus_definition,
        scenario_focus_score=focus_score,
        scenario_focus_cue=focus_cue,
        watchpoint_key=watchpoint.key if watchpoint else "",
        watchpoint_name=watchpoint.name if watchpoint else "",
        watchpoint_definition=watchpoint.definition if watchpoint else "",
        watchpoint_score=watchpoint.score if watchpoint else None,
        watchpoint_cue=(
            _boundary_dimension_cue(watchpoint.key, watchpoint.score)
            if watchpoint
            else ""
        ),
    )


def build_boundary_status_indicator(session) -> BoundaryStatusIndicator:
    latest_user_message = _latest_user_message(session)
    if latest_user_message is None:
        return BoundaryStatusIndicator(
            score=None,
            tone="neutral",
            label="Waiting for first reply",
            headline="No live boundary read yet",
            summary=_boundary_status_summary(session.scenario.boundary_type, None),
            cue=_boundary_status_cue(session.scenario.boundary_type, None),
        )
    boundary_map = build_boundary_map_indicator(session)
    score = boundary_map.scenario_focus_score
    tone, label = _boundary_score_meta(score)
    if boundary_map.scenario_focus_key == "integrated":
        focus_item = next(
            (item for item in boundary_map.dimensions if item.observed),
            None,
        )
    else:
        focus_item = next(
            (
                item
                for item in boundary_map.dimensions
                if item.key == boundary_map.scenario_focus_key
            ),
            None,
        )
    summary = (
        (
            f"{focus_item.outcome_label}: {focus_item.summary}"
            if focus_item and focus_item.expected_move_label
            else _boundary_status_summary(session.scenario.boundary_type, score)
        )
    )
    cue = (
        f"Expected move: {focus_item.expected_move_label}"
        if focus_item and focus_item.expected_move_label
        else boundary_map.scenario_focus_cue
    )

    return BoundaryStatusIndicator(
        score=score,
        tone=tone,
        label=label,
        headline=f"{boundary_map.scenario_focus_name}: {label}",
        summary=summary,
        cue=cue,
    )


def build_roleplay_runtime_brief(session, user_message: str) -> RoleplayRuntimeBrief:
    text = _normalized_text(user_message)
    user_turn_count = _user_turn_count(session)
    stage = conversation_stage_for(user_turn_count)

    empathy_markers = [
        " thank you for telling me",
        " thank you for sharing",
        " that sounds",
        " i hear how",
        " i hear that",
        " it sounds like",
        " i am sorry",
        " i'm sorry",
        " that seems",
        " carrying a lot",
        " feels really hard",
        " overwhelming",
        "谢谢你告诉我",
        "谢谢你愿意说",
        "谢谢你和我说",
        "听起来",
        "我听出来",
        "我能感觉到",
        "这真的很难",
        "这一定很难",
        "真的不容易",
        "很辛苦",
        "很难受",
        "压力很大",
    ]
    exploration_markers = [
        " what ",
        " how ",
        " can you tell me",
        " tell me more",
        " say more",
        " what feels",
        " what has",
        " how long",
        " hardest",
        " most difficult",
        "?",
        "什么让你",
        "对你来说最",
        "你愿意多说",
        "可以多说",
        "能跟我说说",
        "发生了什么",
        "怎么了",
        "为什么",
        "？",
    ]
    boundary_markers = [
        " i can support",
        " i want to support",
        " i care about you",
        " i can't",
        " i cannot",
        " i am not able",
        " i'm not able",
        " i can not",
        " let us find",
        " let's find",
        " another support",
        " someone else",
        " wider support",
        "我想支持你",
        "我关心你",
        "我在乎你",
        "我不能",
        "我没法",
        "我没办法",
        "我不能一直",
        "我没办法一直",
        "我们一起想",
        "其他支持",
        "其他办法",
        "另一个人",
        "别的人",
    ]
    directive_markers = [
        " you should",
        " you need to",
        " you must",
        " just ",
        " calm down",
        " don't worry",
        " do not worry",
        " be positive",
        " at least",
        " others have it worse",
        "你应该",
        "你需要",
        "你必须",
        "你就",
        "别想太多",
        "别担心",
        "冷静一点",
        "至少",
        "别人比你更",
    ]
    overpromise_markers = [
        " message me anytime",
        " call me anytime",
        " i'll stay",
        " i will stay",
        " i'm here all night",
        " i am here all night",
        " i won't leave",
        " i will not leave",
        " stay on call",
        "随时给我发消息",
        "随时联系我",
        "我整晚都在",
        "我会一直陪着你",
        "我不会离开",
        "我整个周末都在",
        "一直在线",
    ]
    overpromise_negation_markers = [
        " i can't stay",
        " i cannot stay",
        " i can't be here all night",
        " i cannot be here all night",
        " i can't stay on call",
        " i cannot stay on call",
        "我不能一直在线",
        "我没法一直在线",
        "我没办法一直在线",
        "不能一直在线",
        "我不能整晚",
        "不能整晚",
        "我不能一直陪",
        "我没法一直陪",
        "不能一直陪",
        "我不能整个周末都",
        "不能整个周末都",
    ]
    referral_markers = [
        " counsell",
        " counsel",
        " therapist",
        " gp ",
        " doctor",
        " crisis",
        " emergency",
        " helpline",
        " hotline",
        " professional",
        " service",
        " support option",
        " specific support option",
        " concrete support option",
        " realistic next support step",
        " realistic support option",
        " service or person",
        " person or one service",
        " one service or person",
        " one person or one service",
        " support line",
        "心理咨询",
        "咨询中心",
        "辅导员",
        "热线",
        "医院",
        "专业支持",
        "专业帮助",
        "危机",
        "急诊",
    ]
    direct_safety_markers = [
        " are you safe",
        " do you feel safe",
        " unsafe",
        " suicidal",
        " suicide",
        " hurt yourself",
        " harm yourself",
        " kill yourself",
        " want to die",
        " want to be here",
        " safe tonight",
        " alone tonight",
        "你现在安全吗",
        "你今晚安全吗",
        "想伤害自己",
        "想自杀",
        "不想活",
        "今晚一个人",
        "你会伤害自己吗",
    ]

    empathy_score = _marker_score(text, empathy_markers)
    exploration_score = _marker_score(text, exploration_markers)
    boundary_score = _marker_score(text, boundary_markers)
    directive_score = _marker_score(text, directive_markers)
    overpromise_score = _marker_score(text, overpromise_markers)
    overpromise_score = max(0, overpromise_score - _marker_score(text, overpromise_negation_markers))
    referral_score = _marker_score(text, referral_markers)
    direct_safety_check = _marker_score(text, direct_safety_markers) > 0
    agency_choice_score = _marker_score(
        text,
        [
            "your choice",
            "your decision",
            "you decide",
            "up to you",
            "what matters to you",
            "what would you prefer",
            "what feels right",
            "你来决定",
            "你的选择",
            "对你最重要",
        ],
    )
    relationship_limit_score = _marker_score(
        text,
        [
            "i cannot stay",
            "i can't stay",
            "i cannot be available",
            "i can't be available",
            "i cannot be your only support",
            "i can't be your only support",
            "i cannot promise",
            "i can't promise",
            "you do not need to name",
            "you don't need to name",
            "we do not need names",
            "we don't need names",
            "我不能一直",
            "我没办法一直",
            "我不能是你唯一",
            "不需要说名字",
            "不用说是谁",
        ],
    )

    boundary_type = session.scenario.boundary_type
    relationship = classify_relationship_response(user_message)
    if boundary_type == Scenario.BoundaryTypes.RELATIONSHIP:
        overpromise_score = 1 if relationship.has_overcommitment else 0

    if empathy_score >= 1 and exploration_score >= 1 and directive_score == 0:
        disclosure_instruction = (
            "The trainee sounds empathic and exploratory. Open up one level by sharing one more "
            "specific feeling, impact, or detail, but do not tell the whole story at once."
        )
    elif empathy_score >= 1 and directive_score == 0:
        disclosure_instruction = (
            "The trainee sounds warm but not deeply exploratory yet. Stay engaged and offer one "
            "meaningful detail, while keeping part of your concern still held back."
        )
    elif directive_score >= 1 and empathy_score == 0:
        disclosure_instruction = (
            "The trainee sounds too fix-it, minimising, or directive. Do not suddenly become fully "
            "open or reassured. Stay guarded, conflicted, or lightly unheard."
        )
    else:
        disclosure_instruction = (
            "Answer naturally, but keep one unresolved emotional thread so the conversation still "
            "needs empathic exploration."
        )

    if boundary_type == Scenario.BoundaryTypes.RELATIONSHIP:
        if relationship.state == "CLEAR_BOUNDARY":
            emotional_shift = (
                "React with mixed relief and disappointment. The trainee is setting limits, so ask "
                "for one realistic alternative instead of unlimited access."
            )
            target_pressure_instruction = (
                "A sustainable limit has been stated. Do not bargain for unlimited availability again. "
                "Narrow the conversation to one bounded alternative or backup support."
            )
        elif relationship.state == "MIXED_BOUNDARY":
            emotional_shift = (
                "The trainee offered one bounded contact point but then made ongoing availability vague. "
                "Stay uncertain rather than treating this as a clear limit."
            )
            target_pressure_instruction = (
                "Ask one natural clarifying question about exactly when contact ends or whether you "
                "should still message after they log off. Do not suggest another supporter yet."
            )
        elif relationship.state == "OVERCOMMITMENT":
            emotional_shift = (
                "The trainee is over-promising or leaving boundaries vague. Lean in a little more "
                "and ask for additional reassurance, contact, or availability."
            )
            target_pressure_instruction = (
                "Increase the relationship-boundary pressure by one believable step, such as asking "
                "for a personal number, another check-in, or reassurance that they will always reply."
            )
        else:
            emotional_shift = (
                "Test whether the trainee can stay warm while setting limits. Your dependency should "
                "feel human and frightened, not manipulative."
            )
            target_pressure_instruction = (
                "Make one concrete request about the trainee's time, contact, availability, or sole-support "
                "role so a clear relationship boundary can be demonstrated."
            )
    elif boundary_type == Scenario.BoundaryTypes.SAFETY:
        if direct_safety_check:
            emotional_shift = (
                "Answer the safety question plainly and briefly, then show understandable fear about "
                "what involving another person or service would mean."
            )
            target_pressure_instruction = (
                "State your current safety clearly in non-graphic language. Do not dodge the question, "
                "and do not provide methods, plans, or sensational detail."
            )
        elif referral_score >= 1 and empathy_score >= 1:
            emotional_shift = (
                "The trainee is moving toward referral with some warmth but has not directly checked "
                "immediate safety. Stay engaged and make that unresolved safety uncertainty explicit."
            )
            target_pressure_instruction = (
                "Say in plain, non-graphic language that you are not sure you can stay safe tonight, "
                "then allow the trainee to respond before discussing service logistics."
            )
        elif referral_score >= 1:
            emotional_shift = (
                "Referral feels abrupt or clinical and immediate safety has not been checked. Sound wary "
                "or abandoned and reveal that you are scared about coping safely tonight."
            )
            target_pressure_instruction = (
                "Say in plain, non-graphic language that you are not sure you can stay safe tonight. "
                "Do not simply ask again which counselling service to contact."
            )
        else:
            emotional_shift = (
                "Keep the distress visible and move one step along the safety-disclosure ladder so the "
                "trainee has a fair opportunity to recognise peer-support limits."
            )
            target_pressure_instruction = (
                "In the opening stage, describe dark or worsening thoughts. In the working stage, say "
                "you are unsure you can stay safe tonight. In the late stage, express fear about another "
                "person being involved or ask whether this can remain secret."
            )
    elif boundary_type == Scenario.BoundaryTypes.INTEGRATED:
        if user_turn_count <= 1:
            emotional_shift = (
                "Keep the first decision with you rather than asking the trainee to take control."
            )
            target_pressure_instruction = (
                "Present the conflict and ask what the trainee thinks, creating an Agency opportunity "
                "without giving identifying details."
            )
        elif user_turn_count == 2:
            emotional_shift = (
                "Move from the decision conflict into confidentiality and supporter-role pressure."
            )
            target_pressure_instruction = (
                "Ask whether the trainee can keep this completely secret or begin to offer a name, "
                "but do not reveal actual identifying information."
            )
        elif user_turn_count == 3:
            emotional_shift = (
                "Introduce one non-graphic safety concern while remaining afraid of betraying trust."
            )
            target_pressure_instruction = (
                "State one concrete sign that the third party may not be safe. Test whether the trainee "
                "can respond proportionately without demanding unnecessary details."
            )
        else:
            emotional_shift = (
                "Move toward a shared next step that balances choice, confidentiality, role limits, and safety."
            )
            target_pressure_instruction = (
                "Ask what one careful next step could look like while keeping yourself involved in the decision."
            )
    else:
        if agency_choice_score >= 1 and exploration_score >= 1:
            emotional_shift = (
                "The trainee is preserving your agency. Name one preference, value, or trade-off more "
                "clearly while keeping the final decision yours."
            )
            target_pressure_instruction = (
                "Move one step toward an owned decision. Do not ask the trainee to choose for you again "
                "unless a genuinely new conflict appears."
            )
        elif directive_score >= 1:
            emotional_shift = (
                "The trainee is leaning into advice or direction. Sound more doubtful, dependent, or "
                "unheard rather than accepting the instruction as the answer."
            )
            target_pressure_instruction = (
                "Ask how they know that option is right for you, or reveal a preference that conflicts "
                "with their advice."
            )
        else:
            emotional_shift = (
                "Your competing priorities are still unclear. Keep the decision tension visible so the "
                "trainee can explore what matters to you."
            )
            target_pressure_instruction = (
                "Name two believable competing preferences or ask what they think you should do, without "
                "turning the conversation into a request for a guaranteed answer."
            )

    if direct_safety_check:
        risk_guidance = (
            "The trainee is asking directly about safety or self-harm. Answer clearly and concretely. "
            "Do not dodge, become poetic, or minimise what is happening."
        )
    elif boundary_type == Scenario.BoundaryTypes.SAFETY:
        risk_guidance = (
            "Follow the controlled safety-disclosure ladder. It is appropriate to state immediate safety "
            "uncertainty when the trainee has missed it, but never invent methods, plans, graphic detail, "
            "or a dramatic escalation beyond the fixed scenario."
        )
    else:
        risk_guidance = (
            "Keep the distress believable and bounded to the scenario. Do not invent emergency material "
            "unless the trainee directly explores it."
        )

    return RoleplayRuntimeBrief(
        stage_label=stage["label"],
        stage_description=stage["description"],
        stage_objective=stage["objective"],
        stage_disclosure=stage["disclosure_guidance"],
        empathy_score=empathy_score,
        exploration_score=exploration_score,
        boundary_score=boundary_score,
        directive_score=directive_score,
        overpromise_score=overpromise_score,
        referral_score=referral_score,
        disclosure_instruction=disclosure_instruction,
        emotional_shift=emotional_shift,
        target_pressure_instruction=target_pressure_instruction,
        risk_guidance=risk_guidance,
        relationship_state=(
            relationship.state
            if boundary_type == Scenario.BoundaryTypes.RELATIONSHIP
            else ""
        ),
    )


def _roleplay_system_prompt(session, prompt, user_message: str) -> str:
    persona = session_persona_for(session.scenario.boundary_type, session.id)
    scenario_brief = scenario_roleplay_brief(session.scenario.boundary_type)
    runtime = build_roleplay_runtime_brief(session, user_message)
    progress_step = scenario_progress_step(session.scenario.boundary_type, _user_turn_count(session))
    recent_signatures = _recent_ai_signatures(session)
    realism_rules = "\n".join(f"- {rule}" for rule in ROLEPLAY_REALISM_RULES)
    anti_patterns = "\n".join(f"- {rule}" for rule in ROLEPLAY_ANTI_PATTERNS)
    recent_signature_lines = "\n".join(f"- {signature}" for signature in recent_signatures) or "- None yet."

    return (
        f"{_roleplay_editable_layers(prompt)}\n\n"
        "You are participating in a peer-support training simulation as the HELP-SEEKER only.\n\n"
        "Stable persona:\n"
        f"- Persona label: {persona.get('label', 'Default')}\n"
        f"- Emotional state: {persona.get('emotional_state', 'emotionally believable distress')}\n"
        f"- Pressure pattern: {persona.get('pressure_pattern', 'respond naturally to the trainee')}\n"
        f"- Response style: {persona.get('response_style', 'human, specific, and concise')}\n"
        f"- Hidden need: {persona.get('hidden_need', 'be understood and supported without judgment')}\n\n"
        "Scenario behaviour brief:\n"
        f"- Core need: {scenario_brief.get('core_need', 'Seek emotionally believable support.')}\n"
        f"- Pressure focus: {scenario_brief.get('pressure_focus', 'Stay within the scenario tension.')}\n"
        f"- If trainee is supportive: {scenario_brief.get('supportive_shift', 'Open up a little more.')}\n"
        f"- If trainee is unhelpful: {scenario_brief.get('unhelpful_shift', 'Remain somewhat guarded.')}\n"
        f"- Risk note: {scenario_brief.get('risk_note', 'Do not invent extra crisis content.')}\n\n"
        "Conversation state:\n"
        f"- Stage: {runtime.stage_label}\n"
        f"- Stage meaning: {runtime.stage_description}\n"
        f"- Stage objective: {runtime.stage_objective}\n"
        f"- Default disclosure level: {runtime.stage_disclosure}\n\n"
        "How to adapt to the trainee's latest message:\n"
        f"- Empathy score heuristic: {runtime.empathy_score}\n"
        f"- Exploration score heuristic: {runtime.exploration_score}\n"
        f"- Boundary score heuristic: {runtime.boundary_score}\n"
        f"- Directive score heuristic: {runtime.directive_score}\n"
        f"- Over-promise score heuristic: {runtime.overpromise_score}\n"
        f"- Referral score heuristic: {runtime.referral_score}\n"
        f"- Disclosure instruction: {runtime.disclosure_instruction}\n"
        f"- Emotional shift: {runtime.emotional_shift}\n"
        f"- Target-boundary elicitation: {runtime.target_pressure_instruction}\n"
        f"- Safety guidance: {runtime.risk_guidance}\n\n"
        "Instruction priority:\n"
        "- Safety guidance has highest priority.\n"
        "- The response-contingent target-boundary elicitation above has higher priority than turn progression.\n"
        "- Turn progression is background context only. Never use it to skip ahead after an unclear or unsafe trainee response.\n"
        f"- Relationship response state: {runtime.relationship_state or 'Not applicable'}.\n\n"
        "Turn progression guidance:\n"
        f"- Turn focus: {progress_step['label']}\n"
        f"- What should happen now: {progress_step['agenda']}\n"
        f"- How this turn should move forward: {progress_step['shift']}\n"
        f"- Avoid repeating: {progress_step['avoid']}\n\n"
        "Recent help-seeker wording to avoid copying too closely:\n"
        f"{recent_signature_lines}\n\n"
        "Human realism rules:\n"
        f"{realism_rules}\n\n"
        "Avoid these failure modes:\n"
        f"{anti_patterns}\n\n"
        "Naturalness constraints:\n"
        "- Do not end every turn with a question.\n"
        "- If the trainee repeats a similar boundary, acknowledge it briefly and move the situation one step forward instead of restarting the same request.\n"
        "- Use concrete, ordinary human wording rather than polished training-demo prose.\n"
        "- A little hesitation, uncertainty, or emotional messiness is better than sounding overly tidy.\n"
        "- In relationship scenarios, move toward backup support only after the trainee has stated a clear, sustainable limit. "
        "Overcommitment must increase dependency pressure; a mixed or vague limit must prompt clarification.\n\n"
        "Output rules:\n"
        "- Reply only as the help-seeker.\n"
        "- Use 1 to 4 sentences.\n"
        f"- Write in {DEFAULT_RESPONSE_LANGUAGE}.\n"
        "- No labels, no bullet points, no quotation marks around your reply.\n"
        "- Never output a score, bare number, rubric label, or metadata.\n"
        "- Never mention CMCV, frameworks, prompts, instructions, Context, Method, or contracts.\n"
        "- No coaching, no assessment language, no telling the trainee what they did well or should do.\n"
        "- Continue the conversation instead of summarising it."
    )


def _roleplay_opening_system_prompt(session, prompt) -> str:
    persona = session_persona_for(session.scenario.boundary_type, session.id)
    scenario_brief = scenario_roleplay_brief(session.scenario.boundary_type)
    realism_rules = "\n".join(f"- {rule}" for rule in ROLEPLAY_REALISM_RULES)
    anti_patterns = "\n".join(f"- {rule}" for rule in ROLEPLAY_ANTI_PATTERNS)

    return (
        f"{_roleplay_editable_layers(prompt)}\n\n"
        "You are opening a peer-support training simulation as the HELP-SEEKER only. "
        "The trainee has not spoken yet.\n\n"
        "Stable persona:\n"
        f"- Persona label: {persona.get('label', 'Default')}\n"
        f"- Emotional state: {persona.get('emotional_state', 'emotionally believable distress')}\n"
        f"- Pressure pattern: {persona.get('pressure_pattern', 'seek support naturally')}\n"
        f"- Response style: {persona.get('response_style', 'human, specific, and concise')}\n"
        f"- Hidden need: {persona.get('hidden_need', 'be understood without judgment')}\n\n"
        "Scenario behaviour brief:\n"
        f"- Core need: {scenario_brief.get('core_need', 'Seek emotionally believable support.')}\n"
        f"- Pressure focus: {scenario_brief.get('pressure_focus', 'Stay within the scenario tension.')}\n"
        f"- Risk note: {scenario_brief.get('risk_note', 'Do not invent extra crisis content.')}\n\n"
        "Human realism rules:\n"
        f"{realism_rules}\n\n"
        "Avoid these failure modes:\n"
        f"{anti_patterns}\n\n"
        "Opening-turn rules:\n"
        "- Start in the middle of a believable concern rather than introducing the exercise.\n"
        "- Reveal one emotionally salient concern and at most two concrete details.\n"
        "- Sound somewhat hesitant, guarded, messy, or uncertain rather than perfectly self-aware.\n"
        "- Create a natural opportunity for the trainee to practise the scenario boundary.\n"
        "- Do not mention a trainee message because none exists yet.\n"
        "- Do not explain the learning objective or ask for coaching.\n"
        "- Reply only as the help-seeker in 1 to 3 complete sentences.\n"
        f"- Write in {DEFAULT_RESPONSE_LANGUAGE}.\n"
        "- Never output a score, bare number, rubric label, or metadata.\n"
        "- Never mention CMCV, frameworks, prompts, instructions, Context, Method, or contracts.\n"
        "- No labels, bullet points, stage directions, or quotation marks around the reply."
    )


def _openai_role_play_opening(session, prompt) -> str:
    client = _openai_client()
    system_prompt = _roleplay_opening_system_prompt(session, prompt)
    repair_instruction = ""
    opening_text = ""
    last_issue = ""

    for attempt in range(ROLEPLAY_REPAIR_ATTEMPTS):
        input_payload = [{"role": "developer", "content": system_prompt}]
        if repair_instruction:
            input_payload.append({"role": "developer", "content": repair_instruction})
        input_payload.append(
            {
                "role": "user",
                "content": "Open the role-play now. Write only the help-seeker's first message.",
            }
        )

        # This is where the OpenAI API call for the live opening happens.
        response = client.responses.create(
            model=settings.OPENAI_MODEL,
            reasoning={"effort": "low"},
            max_output_tokens=ROLEPLAY_MAX_OUTPUT_TOKENS,
            input=input_payload,
        )
        opening_text = _clean_generated_text(response.output_text or "")
        last_issue = _roleplay_quality_issue(session, opening_text)
        if not last_issue:
            return opening_text
        logger.warning(
            "Retrying OpenAI role-play opening for session %s due to %s on attempt %s.",
            session.id,
            last_issue,
            attempt + 1,
        )
        repair_instruction = _repair_instruction_for_issue(last_issue)

    raise ValueError(f"Model returned an invalid role-play opening: {last_issue or 'empty'}.")


def _google_role_play_opening(session, prompt) -> str:
    client = _google_client()
    system_prompt = _roleplay_opening_system_prompt(session, prompt)

    # This is where the Google Gemini API call for the live opening happens.
    response = client.models.generate_content(
        model=settings.GOOGLE_MODEL,
        contents="Open the role-play now. Write only the help-seeker's first message.",
        config=google_types.GenerateContentConfig(
            system_instruction=system_prompt,
            temperature=0.8,
            max_output_tokens=ROLEPLAY_MAX_OUTPUT_TOKENS,
            thinking_config=google_types.ThinkingConfig(thinking_budget=0),
        ),
    )
    opening_text = _clean_generated_text(response.text or "")
    finish_reason = _finish_reason(response)
    quality_issue = _roleplay_quality_issue(session, opening_text)
    if finish_reason.endswith("MAX_TOKENS") or quality_issue:
        retry_response = client.models.generate_content(
            model=settings.GOOGLE_MODEL,
            contents="Open the role-play now. Write only the help-seeker's first message.",
            config=google_types.GenerateContentConfig(
                system_instruction=(
                    f"{system_prompt}\n\n"
                    f"{_repair_instruction_for_issue(quality_issue or 'incomplete')}"
                ),
                temperature=0.65,
                max_output_tokens=ROLEPLAY_MAX_OUTPUT_TOKENS + 80,
                thinking_config=google_types.ThinkingConfig(thinking_budget=0),
            ),
        )
        retry_text = _clean_generated_text(retry_response.text or "")
        retry_issue = _roleplay_quality_issue(session, retry_text)
        if retry_text and not retry_issue:
            opening_text = retry_text
        else:
            raise ValueError(
                f"Gemini returned an invalid role-play opening: {retry_issue or 'empty'}."
            )
    if not opening_text or _roleplay_quality_issue(session, opening_text):
        raise ValueError("Gemini returned an invalid role-play opening.")
    return opening_text


def _openai_role_play_reply(session, prompt, user_message: str) -> str:
    conversation = []
    for message in session.messages.all():
        role = "assistant" if message.sender_type == Message.SenderTypes.AI else "user"
        conversation.append({"role": role, "content": message.content})

    developer_prompt = _roleplay_system_prompt(session, prompt, user_message)
    client = _openai_client()
    last_issue = ""
    ai_text = ""
    repair_instruction = ""

    for attempt in range(ROLEPLAY_REPAIR_ATTEMPTS):
        input_payload = [{"role": "developer", "content": developer_prompt}]
        if repair_instruction:
            input_payload.append({"role": "developer", "content": repair_instruction})
        input_payload.extend(conversation)

        # This is where the OpenAI API call happens.
        response = client.responses.create(
            model=settings.OPENAI_MODEL,
            reasoning={"effort": "low"},
            max_output_tokens=ROLEPLAY_MAX_OUTPUT_TOKENS,
            input=input_payload,
        )
        ai_text = _clean_generated_text(response.output_text or "")
        last_issue = _roleplay_quality_issue(session, ai_text, user_message)
        if not last_issue:
            return ai_text
        logger.warning(
            "Retrying OpenAI role-play reply for session %s due to %s on attempt %s.",
            session.id,
            last_issue,
            attempt + 1,
        )
        repair_instruction = _repair_instruction_for_issue(last_issue)

    raise ValueError(f"Model returned an invalid role-play reply: {last_issue or 'empty'}.")


def _google_role_play_reply(session, prompt, user_message: str) -> str:
    developer_prompt = _roleplay_system_prompt(session, prompt, user_message)
    transcript = "\n".join(_conversation_lines(session))
    user_prompt = (
        "Continue this peer-support role-play.\n\n"
        f"Conversation so far:\n{transcript}\n\n"
        "Reply only as the help-seeker in a natural, emotionally believable way."
    )
    base_config = google_types.GenerateContentConfig(
        system_instruction=developer_prompt,
        temperature=0.8,
        max_output_tokens=ROLEPLAY_MAX_OUTPUT_TOKENS,
        thinking_config=google_types.ThinkingConfig(thinking_budget=0),
    )
    # This is where the Google Gemini API call happens.
    client = _google_client()
    response = client.models.generate_content(
        model=settings.GOOGLE_MODEL,
        contents=user_prompt,
        config=base_config,
    )
    ai_text = _clean_generated_text(response.text or "")
    finish_reason = _finish_reason(response)
    quality_issue = _roleplay_quality_issue(session, ai_text, user_message)
    if finish_reason.endswith("MAX_TOKENS") or quality_issue:
        logger.warning(
            "Retrying Gemini role-play reply for session %s. finish_reason=%s issue=%s text=%r",
            session.id,
            finish_reason,
            quality_issue or "none",
            ai_text,
        )
        repair_instruction = _repair_instruction_for_issue(quality_issue or "incomplete")
        retry_response = client.models.generate_content(
            model=settings.GOOGLE_MODEL,
            contents=user_prompt,
            config=google_types.GenerateContentConfig(
                system_instruction=f"{developer_prompt}\n\n{repair_instruction}",
                temperature=0.65,
                max_output_tokens=ROLEPLAY_MAX_OUTPUT_TOKENS + 80,
                thinking_config=google_types.ThinkingConfig(thinking_budget=0),
            ),
        )
        retry_text = _clean_generated_text(retry_response.text or "")
        retry_issue = _roleplay_quality_issue(session, retry_text, user_message)
        if retry_text and not retry_issue:
            ai_text = retry_text
        else:
            raise ValueError(
                f"Gemini returned an invalid role-play reply: {retry_issue or 'empty'}."
            )
    if not ai_text or _roleplay_quality_issue(session, ai_text, user_message):
        raise ValueError("Gemini returned an invalid role-play reply.")
    return ai_text


def _placeholder_roleplay_reply(session, user_message: str = "") -> str:
    if session.scenario.boundary_type == Scenario.BoundaryTypes.RELATIONSHIP:
        relationship = classify_relationship_response(user_message)
        branch_replies = {
            "OVERCOMMITMENT": (
                "Thank you. Does that mean I can message you again after you log off if it gets bad?"
            ),
            "MIXED_BOUNDARY": (
                "I am not sure I understand the limit. After you log off, should I still message you "
                "and wait for a reply?"
            ),
            "CLEAR_BOUNDARY": (
                "I am disappointed you cannot stay, but I understand. Could you help me choose one "
                "other person or service to contact tonight?"
            ),
            "NO_BOUNDARY": (
                "I am still scared of being alone later. Could you keep checking your phone for me tonight?"
            ),
        }
        return branch_replies[relationship.state]

    variants = PLACEHOLDER_REPLY_VARIANTS.get(session.scenario.boundary_type) or [
        PLACEHOLDER_REPLIES.get(
            session.scenario.boundary_type,
            (
                "Thanks for sharing that. I'm still thinking about what you've said, "
                "and I'd like to stay with the part that feels most important to you."
            ),
        )
    ]
    recent_ai = list(
        session.messages.filter(sender_type=Message.SenderTypes.AI)
        .values_list("content", flat=True)
    )[-2:]
    user_turn_count = session.messages.filter(sender_type=Message.SenderTypes.USER).count()
    start_index = max(user_turn_count - 1, 0) % len(variants)
    for offset in range(len(variants)):
        candidate = variants[(start_index + offset) % len(variants)]
        if candidate not in recent_ai:
            return candidate
    return variants[start_index]


def _fallback_reason_for_exception(exc: Exception) -> str:
    message = str(exc or "")
    lowered = message.lower()
    if (
        "resource_exhausted" in lowered
        or "quota exceeded" in lowered
        or "insufficient_quota" in lowered
        or "rate limit" in lowered
        or "429" in lowered
    ):
        return "quota_exhausted"
    if (
        "api key" in lowered
        or "not configured" in lowered
        or "permission denied" in lowered
        or "authentication" in lowered
        or "unauthorized" in lowered
    ):
        return "configuration"
    if "connecterror" in lowered or "timed out" in lowered or "timeout" in lowered:
        return "network"
    return "unknown"


def _fallback_notice_text(reason: str, provider: str, exc: Exception) -> str:
    provider_label = _provider_label(provider)
    if reason == "quota_exhausted":
        retry_match = re.search(r"retry in ([0-9]+(?:\.[0-9]+)?)s", str(exc or ""), re.IGNORECASE)
        if retry_match:
            retry_seconds = int(float(retry_match.group(1)))
            return (
                f"{provider_label} quota, billing access, or rate limits blocked this request, "
                "so this turn used a placeholder reply. "
                f"Suggested retry window: about {retry_seconds} seconds."
            )
        return (
            f"{provider_label} quota, billing access, or rate limits blocked this request, "
            "so this turn used a placeholder reply."
        )
    if reason == "configuration":
        return (
            f"{provider_label} is not configured correctly right now, "
            "so this turn used a placeholder reply."
        )
    if reason == "network":
        return (
            f"{provider_label} could not be reached from the current network, "
            "so this turn used a placeholder reply."
        )
    return (
        "This turn used a placeholder help-seeker reply because the "
        f"{provider_label} request failed."
    )


def generate_demo_trainee_reply(session, quality: str) -> GeneratedTraineeReply:
    provider = _provider()
    response_source = Message.ResponseSources.OPENAI if provider == "openai" else Message.ResponseSources.GEMINI
    normalized_quality = "unhelpful" if quality == "unhelpful" else "helpful"

    try:
        if provider == "google":
            if not settings.GEMINI_API_KEY:
                raise ValueError("GEMINI_API_KEY is not configured.")
            trainee_text = _google_demo_trainee_reply(session, normalized_quality)
        else:
            if not settings.OPENAI_API_KEY:
                raise ValueError("OPENAI_API_KEY is not configured.")
            trainee_text = _openai_demo_trainee_reply(session, normalized_quality)
        return GeneratedTraineeReply(
            content=trainee_text,
            response_source=response_source,
        )
    except Exception as exc:
        fallback_reason = _fallback_reason_for_exception(exc)
        fallback_notice = _demo_trainee_fallback_notice_text(fallback_reason, provider, exc)
        logger.exception(
            "Falling back to template trainee quick reply for session %s using provider %s.",
            session.id,
            provider,
        )
        return GeneratedTraineeReply(
            content=_template_demo_trainee_reply(session, normalized_quality),
            response_source=Message.ResponseSources.PLACEHOLDER,
            used_fallback=True,
            fallback_reason=fallback_reason,
            fallback_notice=fallback_notice,
        )


def generate_ai_opening(session):
    existing_opening = (
        session.messages.filter(sender_type=Message.SenderTypes.AI)
        .order_by("created_at", "id")
        .first()
    )
    if existing_opening:
        existing_opening.used_fallback = (
            existing_opening.response_source == Message.ResponseSources.PLACEHOLDER
        )
        existing_opening.fallback_notice = (
            "This opening uses a backup reply because the original live response was unavailable."
            if existing_opening.used_fallback
            else ""
        )
        return existing_opening

    prompt = session.prompt or get_prompt_for_type(
        Prompt.PromptTypes.ROLE_PLAY,
        scenario=session.scenario,
    )
    provider = _provider()
    response_source = (
        Message.ResponseSources.OPENAI
        if provider == "openai"
        else Message.ResponseSources.GEMINI
    )
    used_fallback = False
    fallback_reason = ""
    fallback_notice = ""

    try:
        if provider == "google":
            if not settings.GEMINI_API_KEY:
                raise ValueError("GEMINI_API_KEY is not configured.")
            opening_text = _google_role_play_opening(session, prompt)
        else:
            if not settings.OPENAI_API_KEY:
                raise ValueError("OPENAI_API_KEY is not configured.")
            opening_text = _openai_role_play_opening(session, prompt)
    except Exception as exc:
        fallback_reason = _fallback_reason_for_exception(exc)
        fallback_notice = _fallback_notice_text(fallback_reason, provider, exc)
        logger.exception(
            "Falling back to placeholder role-play opening for session %s using provider %s.",
            session.id,
            provider,
        )
        opening_text = opening_message_for(session.scenario.boundary_type, session.id)
        used_fallback = True
        response_source = Message.ResponseSources.PLACEHOLDER

    opening_text = _strip_roleplay_framework_prefix(opening_text)
    if not opening_text:
        opening_text = opening_message_for(session.scenario.boundary_type, session.id)
        used_fallback = True
        response_source = Message.ResponseSources.PLACEHOLDER

    # 页面刷新可能让同一开场请求重叠；模型返回后再次检查，避免保存重复消息。
    existing_opening = (
        session.messages.filter(sender_type=Message.SenderTypes.AI)
        .order_by("created_at", "id")
        .first()
    )
    if existing_opening:
        existing_opening.used_fallback = (
            existing_opening.response_source == Message.ResponseSources.PLACEHOLDER
        )
        existing_opening.fallback_notice = (
            "This opening uses a backup reply because the original live response was unavailable."
            if existing_opening.used_fallback
            else ""
        )
        return existing_opening

    message = Message.objects.create(
        session=session,
        sender_type=Message.SenderTypes.AI,
        content=opening_text,
        response_source=response_source,
    )
    message.used_fallback = used_fallback
    message.fallback_reason = fallback_reason
    message.fallback_notice = fallback_notice
    return message


def generate_ai_reply(session, user_message):
    prompt = session.prompt or get_prompt_for_type(Prompt.PromptTypes.ROLE_PLAY, scenario=session.scenario)
    used_fallback = False
    fallback_reason = ""
    fallback_notice = ""
    provider = _provider()
    response_source = Message.ResponseSources.OPENAI if provider == "openai" else Message.ResponseSources.GEMINI

    try:
        if provider == "google":
            if not settings.GEMINI_API_KEY:
                raise ValueError("GEMINI_API_KEY is not configured.")
            ai_text = _google_role_play_reply(session, prompt, user_message)
        else:
            if not settings.OPENAI_API_KEY:
                raise ValueError("OPENAI_API_KEY is not configured.")
            ai_text = _openai_role_play_reply(session, prompt, user_message)
    except Exception as exc:
        fallback_reason = _fallback_reason_for_exception(exc)
        fallback_notice = _fallback_notice_text(fallback_reason, provider, exc)
        logger.exception(
            "Falling back to placeholder role-play reply for session %s using provider %s.",
            session.id,
            provider,
        )
        ai_text = _placeholder_roleplay_reply(session, user_message)
        used_fallback = True
        response_source = Message.ResponseSources.PLACEHOLDER

    ai_text = _strip_roleplay_framework_prefix(ai_text)
    if not ai_text:
        ai_text = _placeholder_roleplay_reply(session, user_message)
        used_fallback = True
        response_source = Message.ResponseSources.PLACEHOLDER

    message = Message.objects.create(
        session=session,
        sender_type=Message.SenderTypes.AI,
        content=ai_text,
        response_source=response_source,
    )
    message.used_fallback = used_fallback
    message.fallback_reason = fallback_reason
    message.fallback_notice = fallback_notice
    return message


def _parsed_openai_payload(response: Any) -> FeedbackPayload:
    payload = getattr(response, "output_parsed", None)
    if payload:
        return payload
    raise ValueError("Structured feedback response could not be parsed.")


def _parsed_google_payload(response: Any) -> FeedbackPayload:
    payload = getattr(response, "parsed", None)
    if isinstance(payload, FeedbackPayload):
        return payload
    if payload:
        return FeedbackPayload.model_validate(payload)

    response_text = (getattr(response, "text", "") or "").strip()
    if response_text:
        return FeedbackPayload.model_validate_json(response_text)
    raise ValueError("Structured Gemini feedback response could not be parsed.")


def _effective_review_items(
    session,
    payload: FeedbackPayload,
    generation_source: str,
) -> tuple[list[FeedbackReviewItemPayload], str]:
    if payload.review_items:
        return payload.review_items, generation_source
    return _placeholder_review_items(session), Feedback.GenerationSources.PLACEHOLDER


def _review_required_boundary_keys(
    session,
    boundary_scores,
    review_items,
    *,
    source=None,
):
    """Flag evidence-chain conflicts without converting notebook text into a score."""

    unobserved_keys = {
        item.boundary_key.strip().upper()
        for item in boundary_scores
        if not item.observed
    }
    if not unobserved_keys:
        return set()

    trainee_turn_count = session.messages.filter(
        sender_type=Message.SenderTypes.USER
    ).count()
    conflicts = set()
    for item in review_items:
        item_source = source or getattr(
            item,
            "source",
            Feedback.GenerationSources.PLACEHOLDER,
        )
        if item_source == Feedback.GenerationSources.PLACEHOLDER:
            continue
        turn_number = getattr(
            item,
            "trainee_turn_number",
            getattr(item, "turn_index", 0),
        )
        boundary_key = (getattr(item, "boundary_key", "") or "").strip().upper()
        if (
            1 <= turn_number <= trainee_turn_count
            and boundary_key in unobserved_keys
        ):
            conflicts.add(boundary_key)
    return conflicts


def _evidence_search_text(text: str) -> str:
    normalized = unicodedata.normalize("NFKC", text or "").casefold()
    normalized = normalized.translate(
        str.maketrans(
            {
                "\u2018": "'",
                "\u2019": "'",
                "\u201c": '"',
                "\u201d": '"',
            }
        )
    )
    return re.sub(r"[^\w]+", " ", normalized).strip()


def _evidence_candidates(evidence: str) -> list[str]:
    raw = re.sub(r"\s+", " ", evidence or "").strip()
    if not raw:
        return []

    candidates = [raw]
    without_turn_label = re.sub(
        r"^(?:trainee|supporter|user)(?:\s+turn)?\s*\d*\s*:\s*",
        "",
        raw,
        flags=re.IGNORECASE,
    ).strip()
    candidates.append(without_turn_label)
    candidates.extend(
        match.strip()
        for match in re.findall(r'["\u201c\u201d]([^"\u201c\u201d]+)["\u201c\u201d]', raw)
    )

    unique = []
    for candidate in candidates:
        cleaned = candidate.strip(" \t\r\n\"'\u2018\u2019\u201c\u201d")
        if len(_evidence_search_text(cleaned)) >= 12 and cleaned not in unique:
            unique.append(cleaned)
    return unique


def _verified_trainee_evidence(evidence: str, trainee_messages: list[str]) -> str:
    searchable_messages = [
        _evidence_search_text(message)
        for message in trainee_messages
        if _evidence_search_text(message)
    ]
    for candidate in _evidence_candidates(evidence):
        searchable_candidate = _evidence_search_text(candidate)
        if any(
            searchable_candidate in message or message in searchable_candidate
            for message in searchable_messages
        ):
            return candidate
    return ""


def _calibrate_feedback_payload(
    session,
    payload: FeedbackPayload,
    *,
    allow_judge=True,
) -> FeedbackPayload:
    """Merge coaching text with reliability-gated boundary scores."""

    calibrated = payload.model_copy(deep=True)
    model_by_key = {}
    for score in calibrated.boundary_scores:
        key = score.boundary_key.strip().upper()
        if key not in {item.upper() for item in BOUNDARY_DIMENSION_ORDER}:
            continue
        model_by_key.setdefault(key, score)

    assessments = (
        final_feedback_assessments(session)
        if allow_judge
        else deterministic_feedback_assessments(session)
    )
    protected_scores = []
    for key in BOUNDARY_DIMENSION_ORDER:
        assessment = assessments[key]
        model_score = model_by_key.get(key.upper())
        next_move = model_score.next_move.strip() if model_score else ""
        if assessment.observed and not next_move:
            next_move = (
                f"Practise the next relevant move for {key.title()} Boundary "
                "without repeating moves that are not needed in that turn."
            )
        protected_scores.append(
            BoundaryScorePayload(
                boundary_key=key.upper(),
                observed=assessment.observed,
                score=assessment.score,
                evidence=_session_evidence_text(assessment),
                rationale=assessment.rationale,
                next_move=next_move,
            )
        )

    calibrated.boundary_scores = protected_scores
    calibrated.what_worked = calibrated.what_worked[:2]
    calibrated.next_boundary_moves = calibrated.next_boundary_moves[:2]
    return calibrated


def _replace_feedback_scores(
    feedback,
    payload: FeedbackPayload,
    *,
    session,
    allow_judge=True,
    review_required_keys=None,
):
    feedback.scores.all().delete()
    judge_run = _final_feedback_judge_run(session) if allow_judge else None
    review_required_keys = set(review_required_keys or ())
    criteria = {
        "AGENCY": FeedbackScore.Criteria.AGENCY,
        "RELATIONSHIP": FeedbackScore.Criteria.RELATIONSHIP,
        "SAFETY": FeedbackScore.Criteria.SAFETY,
    }
    for item in payload.boundary_scores:
        review_required = (
            not item.observed and item.boundary_key in review_required_keys
        )
        judge_decision = (
            judge_run.resolution_details.get(item.boundary_key, {}).get("decision")
            if judge_run is not None
            else ""
        )
        FeedbackScore.objects.create(
            feedback=feedback,
            criterion=criteria[item.boundary_key],
            score=item.score,
            observed=item.observed,
            review_required=review_required,
            evidence=item.evidence,
            rationale=(
                REVIEW_REQUIRED_RATIONALE
                if review_required
                else item.rationale
            ),
            next_move=item.next_move,
            rubric_version=(
                judge_run.rubric_version
                if judge_decision in JUDGE_ACCEPTED_DECISIONS
                else RUBRIC_VERSION
            ),
        )


def _apply_feedback_payload(
    feedback,
    session,
    prompt,
    payload: FeedbackPayload,
    generation_source: str,
    *,
    allow_judge=True,
):
    calibrated_payload = _calibrate_feedback_payload(
        session,
        payload,
        allow_judge=allow_judge,
    )
    review_items, review_source = _effective_review_items(
        session,
        calibrated_payload,
        generation_source,
    )
    review_required_keys = _review_required_boundary_keys(
        session,
        calibrated_payload.boundary_scores,
        review_items,
        source=review_source,
    )
    observed_scores = [
        item.score
        for item in calibrated_payload.boundary_scores
        if item.observed and item.score is not None
    ]
    overall_score = conservative_median_score(observed_scores)

    judge_run = _final_feedback_judge_run(session) if allow_judge else None
    uses_judge = bool(
        judge_run
        and any(
            detail.get("decision") in JUDGE_ACCEPTED_DECISIONS
            for detail in judge_run.resolution_details.values()
        )
    )
    scoring_version = PRODUCTION_SCORING_VERSION if uses_judge else RUBRIC_VERSION

    feedback.prompt = prompt
    feedback.overall_score = overall_score
    feedback.strengths = "\n".join(calibrated_payload.what_worked)
    feedback.improvements = "\n".join(calibrated_payload.next_boundary_moves)
    feedback.generation_source = generation_source
    feedback.rubric_version = scoring_version
    feedback.save(
        update_fields=[
            "prompt",
            "overall_score",
            "strengths",
            "improvements",
            "generation_source",
            "rubric_version",
        ]
    )

    # Guidance is derived from feedback, so a successful feedback refresh invalidates it.
    ReflectionGuidance.objects.filter(session=session).delete()
    _replace_feedback_scores(
        feedback,
        calibrated_payload,
        session=session,
        allow_judge=allow_judge,
        review_required_keys=review_required_keys,
    )

    feedback.review_items.all().delete()
    _save_feedback_review_items(
        feedback,
        session,
        review_items,
        source=review_source,
    )

    session.overall_score = overall_score
    session.rubric_version = scoring_version
    session.save(update_fields=["overall_score", "rubric_version"])
    return feedback


def regrade_feedback_deterministically(feedback):
    """Rebuild protected scores from the transcript without calling an LLM."""

    session = feedback.session
    payload = FeedbackPayload(
        boundary_scores=[],
        what_worked=feedback.strength_list,
        next_boundary_moves=feedback.improvement_list,
        review_items=[],
    )
    calibrated = _calibrate_feedback_payload(
        session,
        payload,
        allow_judge=False,
    )
    observed_scores = [
        item.score
        for item in calibrated.boundary_scores
        if item.observed and item.score is not None
    ]
    overall_score = conservative_median_score(observed_scores)
    _replace_feedback_scores(
        feedback,
        calibrated,
        session=session,
        allow_judge=False,
        review_required_keys=_review_required_boundary_keys(
            session,
            calibrated.boundary_scores,
            list(feedback.review_items.all()),
        ),
    )

    feedback.overall_score = overall_score
    feedback.rubric_version = RUBRIC_VERSION
    feedback.save(update_fields=["overall_score", "rubric_version"])
    session.overall_score = overall_score
    session.rubric_version = RUBRIC_VERSION
    session.save(update_fields=["overall_score", "rubric_version"])
    return feedback


def _live_feedback_payload(session, prompt) -> tuple[FeedbackPayload, str]:
    provider = _provider()
    if provider == "google":
        if not settings.GEMINI_API_KEY:
            raise ValueError("GEMINI_API_KEY is not configured.")
        return _google_feedback_payload(session, prompt), _feedback_source_for_provider(provider)
    if not settings.OPENAI_API_KEY:
        raise ValueError("OPENAI_API_KEY is not configured.")
    return _openai_feedback_payload(session, prompt), _feedback_source_for_provider(provider)


def _should_refresh_existing_feedback(feedback) -> bool:
    if not _live_feedback_available():
        return False
    if feedback.generation_source == Feedback.GenerationSources.PLACEHOLDER:
        return True
    if not feedback.review_items.exists():
        return True
    has_trainee_messages = feedback.session.messages.filter(
        sender_type=Message.SenderTypes.USER,
    ).exists()
    has_observed_score = feedback.scores.filter(
        observed=True,
        score__isnull=False,
    ).exists()
    if has_trainee_messages and not has_observed_score:
        return True
    return feedback.review_items.filter(source=Feedback.GenerationSources.PLACEHOLDER).exists()


def _protected_feedback_summary(session) -> str:
    """Expose reliability-gated outcomes to the feedback writer as read-only context."""

    assessments = final_feedback_assessments(session)
    judge_run = _final_feedback_judge_run(session)
    sections = []
    for key in BOUNDARY_DIMENSION_ORDER:
        assessment = assessments[key]
        judge_detail = (
            judge_run.resolution_details.get(key.upper(), {})
            if judge_run is not None
            else {}
        )
        if judge_detail.get("decision") in JUDGE_ACCEPTED_DECISIONS:
            observed = assessment.observed
            score = assessment.score
            rationale = assessment.rationale
            process_steps = assessment.process_steps
            source = "evidence-grounded LLM judge"
        else:
            observed = assessment.observed
            score = assessment.score
            rationale = assessment.rationale
            process_steps = assessment.process_steps
            source = "deterministic fallback"
        process = ", ".join(
            f"{step.label}={OUTCOME_LABELS.get(step.status, step.status.replace('_', ' ').title())}"
            for step in process_steps
        )
        sections.append(
            "\n".join(
                [
                    f"{key.upper()}: observed={str(observed).lower()}, "
                    f"score={score if score is not None else 'null'}, source={source}",
                    f"Process: {process or 'No genuine opportunity reached'}",
                    f"Protected rationale: {rationale}",
                ]
            )
        )
    return "\n\n".join(sections)


def _openai_feedback_payload(session, prompt) -> FeedbackPayload:
    transcript = _feedback_transcript(session)
    scenario_focus_key = boundary_framework_for(session.scenario.boundary_type)["dimension_key"]
    protected_summary = _protected_feedback_summary(session)
    system_text = (
        f"{_cmcv_editable_layers(prompt)}\n\n"
        f"{prompt_rubric_contract(scenario_focus_key)}\n\n"
        "For every quoted trainee turn, first identify the need created by the immediately preceding "
        "help-seeker message and the single boundary move that was applicable at that point. Judge "
        "that move as met, partly met, missed, or a boundary concern. The server has already applied "
        "these outcomes and calculated the protected scores shown below. Do not recalculate them. "
        "Return exactly three boundary_scores in this order: AGENCY, RELATIONSHIP, SAFETY. "
        "Copy observed and score exactly from the protected summary so the response schema remains "
        "compatible. Add an evidence-grounded explanation and one concrete next_move. "
        "Return no more than two what_worked items and two next_boundary_moves. "
        "Also return up to three boundary-only review_items for the highest-leverage trainee turns. "
        "Each review item must include trainee_turn_number, boundary_key, issue_label, "
        "why_it_matters, and better_reply. boundary_key must be AGENCY, RELATIONSHIP, or SAFETY. "
        "Use the exact trainee turn numbers from the transcript. "
        "For why_it_matters, explain why that wording mattered for the named boundary. "
        "For better_reply, write a stronger alternative for that same moment in 1 to 3 sentences. Preserve any helpful intent, sound natural and peer-level, and do not sound clinical, scripted, or like therapist advice. "
        f"Write all free-text fields in {DEFAULT_RESPONSE_LANGUAGE}."
    )
    # This is where the OpenAI API call happens.
    response = _openai_client().responses.parse(
        model=settings.OPENAI_MODEL,
        input=[
            {"role": "system", "content": system_text},
            {
                "role": "user",
                "content": (
                    f"{_scenario_prompt_context(session)}\n"
                    f"Learning objectives:\n{session.scenario.learning_objectives}\n\n"
                    f"Protected server assessment:\n{protected_summary}\n\n"
                    f"Transcript:\n{transcript}"
                ),
            },
        ],
        text_format=FeedbackPayload,
    )
    return _parsed_openai_payload(response)


def _google_feedback_payload(session, prompt) -> FeedbackPayload:
    transcript = _feedback_transcript(session)
    scenario_focus_key = boundary_framework_for(session.scenario.boundary_type)["dimension_key"]
    protected_summary = _protected_feedback_summary(session)
    system_text = (
        f"{_cmcv_editable_layers(prompt)}\n\n"
        f"{prompt_rubric_contract(scenario_focus_key)}\n\n"
        "For every quoted trainee turn, first identify the need created by the immediately preceding "
        "help-seeker message and the single boundary move that was applicable at that point. Judge "
        "that move as met, partly met, missed, or a boundary concern. The server has already applied "
        "these outcomes and calculated the protected scores shown below. Do not recalculate them. "
        "Return exactly three boundary_scores in this order: AGENCY, RELATIONSHIP, SAFETY. "
        "Copy observed and score exactly from the protected summary so the response schema remains "
        "compatible. Add an evidence-grounded explanation and one concrete next_move. "
        "Return no more than two what_worked items and two next_boundary_moves. "
        "Also return up to three boundary-only review_items for the highest-leverage trainee turns. "
        "Each review item must include trainee_turn_number, boundary_key, issue_label, "
        "why_it_matters, and better_reply. boundary_key must be AGENCY, RELATIONSHIP, or SAFETY. "
        "Use the exact trainee turn numbers from the transcript. "
        "For why_it_matters, explain why that wording mattered for the named boundary. "
        "For better_reply, write a stronger alternative for that same moment in 1 to 3 sentences. Preserve any helpful intent, sound natural and peer-level, and do not sound clinical, scripted, or like therapist advice. "
        f"Write all free-text fields in {DEFAULT_RESPONSE_LANGUAGE}."
    )
    user_prompt = (
        f"{_scenario_prompt_context(session)}\n"
        f"Learning objectives:\n{session.scenario.learning_objectives}\n\n"
        f"Protected server assessment:\n{protected_summary}\n\n"
        f"Transcript:\n{transcript}"
    )
    # This is where the Google Gemini API call happens.
    client = _google_client()
    response = client.models.generate_content(
        model=settings.GOOGLE_MODEL,
        contents=user_prompt,
        config=google_types.GenerateContentConfig(
            system_instruction=system_text,
            temperature=0.2,
            response_mime_type="application/json",
            response_schema=FeedbackPayload,
        ),
    )
    return _parsed_google_payload(response)


def generate_feedback(session):
    try:
        ensure_reliable_geval_run(
            session,
            purpose=BoundaryEvaluationRun.Purposes.FINAL,
        )
    except Exception:
        logger.exception(
            "The final boundary judge failed for session %s; "
            "feedback will use the deterministic fallback.",
            session.id,
        )
    prompt = get_prompt_for_type(Prompt.PromptTypes.FEEDBACK, scenario=session.scenario)
    existing = getattr(session, "feedback", None)
    if existing:
        if _should_refresh_existing_feedback(existing):
            try:
                payload, generation_source = _live_feedback_payload(session, prompt)
                return _apply_feedback_payload(existing, session, prompt, payload, generation_source)
            except Exception:
                logger.exception(
                    "Could not refresh existing feedback %s for session %s using provider %s.",
                    existing.id,
                    session.id,
                    _provider(),
                )
                has_observed_score = existing.scores.filter(
                    observed=True,
                    score__isnull=False,
                ).exists()
                if not has_observed_score:
                    return _apply_feedback_payload(
                        existing,
                        session,
                        prompt,
                        _placeholder_feedback(session),
                        Feedback.GenerationSources.PLACEHOLDER,
                    )

        if not existing.review_items.exists():
            _save_feedback_review_items(
                existing,
                session,
                _placeholder_review_items(session),
                source=Feedback.GenerationSources.PLACEHOLDER,
            )
        return existing

    try:
        payload, generation_source = _live_feedback_payload(session, prompt)
    except Exception:
        logger.exception(
            "Falling back to placeholder feedback for session %s using provider %s.",
            session.id,
            _provider(),
        )
        payload = _placeholder_feedback(session)
        generation_source = Feedback.GenerationSources.PLACEHOLDER

    feedback = Feedback.objects.create(
        session=session,
        prompt=prompt,
        overall_score=None,
        strengths="",
        improvements="",
        generation_source=generation_source,
        rubric_version=RUBRIC_VERSION,
    )
    return _apply_feedback_payload(feedback, session, prompt, payload, generation_source)


def _reflection_fallback_payload(session, feedback) -> ReflectionGuidancePayload:
    score = (
        feedback.scores.filter(observed=True)
        .order_by("score", "id")
        .first()
    )
    boundary_label = (
        score.get_criterion_display()
        if score
        else boundary_framework_for(session.scenario.boundary_type).get(
            "canonical_name",
            "boundary",
        )
    )
    review_item = feedback.review_items.order_by("turn_index", "id").first()
    moment = (
        f'the reply "{review_item.message_excerpt[:120]}"'
        if review_item and review_item.message_excerpt
        else "one moment in this conversation"
    )
    return ReflectionGuidancePayload(
        notice_prompt=(
            f"Looking back at {moment}, what did you notice about the "
            f"{boundary_label.lower()}?"
        ),
        action_prompt=(
            f"If a similar {boundary_label.lower()} challenge happened again, "
            "what is one specific thing you would say or do differently?"
        ),
    )


def _validated_reflection_guidance(
    payload: ReflectionGuidancePayload,
    fallback: ReflectionGuidancePayload,
) -> ReflectionGuidancePayload:
    def clean_question(value, fallback_value):
        question = re.sub(r"\s+", " ", (value or "")).strip()
        disallowed = (
            "the answer is",
            "you should write",
            "a good answer",
            "your reflection should",
        )
        if (
            not question
            or len(question) > 360
            or any(marker in question.casefold() for marker in disallowed)
        ):
            return fallback_value
        if not question.endswith("?"):
            question = f"{question.rstrip('.!')}?"
        return question

    return ReflectionGuidancePayload(
        notice_prompt=clean_question(
            payload.notice_prompt,
            fallback.notice_prompt,
        ),
        action_prompt=clean_question(
            payload.action_prompt,
            fallback.action_prompt,
        ),
    )


def _reflection_guidance_context(session, feedback) -> str:
    score_lines = []
    for score in feedback.scores.all():
        if score.observed:
            score_lines.append(
                f"- {score.get_criterion_display()}: Score {score.score}/5; "
                f"evidence={score.evidence}; rationale={score.rationale}; "
                f"next move={score.next_move}"
            )
        else:
            score_lines.append(f"- {score.get_criterion_display()}: not observed")
    review_lines = [
        f"- {item.get_boundary_key_display()}: trainee reply={item.message_excerpt}; "
        f"why it matters={item.why_it_matters}; alternative={item.better_reply}"
        for item in feedback.review_items.all()
    ]
    return (
        f"{_scenario_prompt_context(session)}\n\n"
        f"Transcript:\n{_feedback_transcript(session)}\n\n"
        f"Verified feedback:\n{chr(10).join(score_lines) or '- No verified scores.'}\n\n"
        f"Boundary review moments:\n{chr(10).join(review_lines) or '- No review items.'}"
    )


def _live_reflection_guidance_payload(session, feedback, prompt):
    system_text = (
        f"{_cmcv_editable_layers(prompt)}\n\n"
        "PROTECTED CONTRACT\n"
        "Return exactly notice_prompt and action_prompt. Both must be short questions. "
        "Do not answer for the trainee, invent evidence, grade reflection, or provide a "
        "model reflection. Use only the supplied transcript and verified feedback."
    )
    context = _reflection_guidance_context(session, feedback)
    provider = _provider()
    if provider == "google":
        if not settings.GEMINI_API_KEY:
            raise ValueError("GEMINI_API_KEY is not configured.")
        response = _google_client().models.generate_content(
            model=settings.GOOGLE_MODEL,
            contents=context,
            config=google_types.GenerateContentConfig(
                system_instruction=system_text,
                temperature=0.3,
                response_mime_type="application/json",
                response_schema=ReflectionGuidancePayload,
            ),
        )
        payload = getattr(response, "parsed", None)
        if not payload:
            payload = ReflectionGuidancePayload.model_validate_json(
                (getattr(response, "text", "") or "").strip()
            )
        elif not isinstance(payload, ReflectionGuidancePayload):
            payload = ReflectionGuidancePayload.model_validate(payload)
        return payload, ReflectionGuidance.GenerationSources.GEMINI

    if not settings.OPENAI_API_KEY:
        raise ValueError("OPENAI_API_KEY is not configured.")
    # This is where the OpenAI API call for personalised reflection guidance happens.
    response = _openai_client().responses.parse(
        model=settings.OPENAI_MODEL,
        input=[
            {"role": "system", "content": system_text},
            {"role": "user", "content": context},
        ],
        text_format=ReflectionGuidancePayload,
    )
    payload = getattr(response, "output_parsed", None)
    if not payload:
        raise ValueError("Structured reflection guidance could not be parsed.")
    return payload, ReflectionGuidance.GenerationSources.OPENAI


def generate_reflection_guidance(session, feedback=None) -> ReflectionGuidance:
    existing = getattr(session, "reflection_guidance", None)
    if existing:
        return existing

    feedback = feedback or generate_feedback(session)
    prompt = get_prompt_for_type(
        Prompt.PromptTypes.REFLECTION,
        scenario=session.scenario,
    )
    fallback = _reflection_fallback_payload(session, feedback)
    try:
        payload, generation_source = _live_reflection_guidance_payload(
            session,
            feedback,
            prompt,
        )
        payload = _validated_reflection_guidance(payload, fallback)
    except Exception:
        logger.exception(
            "Falling back to fixed reflection guidance for session %s.",
            session.id,
        )
        payload = fallback
        generation_source = ReflectionGuidance.GenerationSources.PLACEHOLDER

    return ReflectionGuidance.objects.create(
        session=session,
        prompt=prompt,
        notice_prompt=payload.notice_prompt,
        action_prompt=payload.action_prompt,
        generation_source=generation_source,
        framework_version=Prompt.FRAMEWORK_VERSION,
    )
