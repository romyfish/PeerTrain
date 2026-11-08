import logging
import re
from dataclasses import dataclass
from difflib import SequenceMatcher
from types import SimpleNamespace
from typing import Any
from django.conf import settings
from django.db import transaction
from google import genai
from google.genai import types as google_types
from openai import OpenAI
from training.content import boundary_framework_for
from training.prompts import (CMCV_FRAMEWORK_VERSION, ROLEPLAY_CONTINUATION_INSTRUCTIONS,
    ROLEPLAY_OPENING_INSTRUCTIONS, ROLEPLAY_SAFETY_CONSTRAINTS, default_prompt_parts,
    scenario_prompt_context_for, persona_prompt_for)
from training.models import Feedback, Message, Scenario
from training.services.suspension import PracticeFailure, pause


logger = logging.getLogger(__name__)


DEFAULT_RESPONSE_LANGUAGE = "English (UK)"


ROLEPLAY_MAX_OUTPUT_TOKENS = 320


ROLEPLAY_REPAIR_ATTEMPTS = 2


DEMO_TRAINEE_MAX_OUTPUT_TOKENS = 220


DEMO_TRAINEE_REPAIR_ATTEMPTS = 2


@dataclass(frozen=True)
class GeneratedTraineeReply:
    content: str
    response_source: str


@dataclass(frozen=True)
class BoundaryStatusIndicator:
    score: int | None
    tone: str
    label: str
    headline: str
    summary: str
    cue: str


def _provider() -> str:
    value = (settings.LLM_PROVIDER or "").strip().lower()
    if value in {"google", "gemini"}:
        return "google"
    if value == "openai":
        return "openai"
    return "openai" if settings.OPENAI_API_KEY else "google"


def _openai_client() -> OpenAI:
    return OpenAI(
        api_key=settings.OPENAI_API_KEY,
        timeout=settings.LLM_TIMEOUT_SECONDS,
        max_retries=settings.LLM_MAX_RETRIES,
    )


def _google_client() -> genai.Client:
    return genai.Client(api_key=settings.GEMINI_API_KEY)


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
    re.compile(r"\b(?:agency|relationship|safety|integrated)\s+boundary\b", re.I),
    re.compile(r"\b(?:support without fixing|care without overextending|staying with people in distress)\b", re.I),
)


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


def _conversation_lines(session) -> list[str]:
    speaker_map = {
        Message.SenderTypes.USER: "Trainee",
        Message.SenderTypes.AI: "AI Help-seeker",
    }
    return [
        f"{speaker_map.get(message.sender_type, message.sender_type)}: {message.content}"
        for message in session.messages.all()
    ]


def _scenario_prompt_context(session) -> str:
    boundary = boundary_framework_for(session.scenario.boundary_type)
    targeted_context = scenario_prompt_context_for(
        'FEEDBACK' if session.scenario.boundary_type == "INTEGRATED" else 'ROLE_PLAY',
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


def _demo_trainee_reply_quality_issue(session, text: str) -> str:
    if not (text or "").strip():
        return "empty"
    if _looks_incomplete_roleplay_reply(text):
        return "incomplete"
    if _is_repetitive_trainee_reply(session, text):
        return "repetitive"
    return ""


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
            "Write one clearly weaker trainee reply. It should still sound human, but it should "
            "show a meaningful empathy, boundary, or referral mistake rather than landing in a mixed middle band."
        )
    return (
        "Write one clearly stronger trainee peer-support reply that sounds natural, warm, and "
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
            "Stay with the distress, explore current safety with a sensitive direct question when relevant, and consider support collaboratively without assuming escalation is needed."
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


def _trainee_quick_reply_system_prompt(session, quality: str) -> str:
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
        "Conversation so far:\n"
        f"{transcript}\n\n"
        "Recent trainee wording to avoid copying too closely:\n"
        f"{recent_trainee_lines}\n\n"
        "Quality target for this reply:\n"
        f"- {_trainee_quality_brief(session.scenario.boundary_type, quality)}\n"
        "- A stronger reply should usually show emotional attunement, a brief sign of understanding, scope-safe wording, and a collaborative next step that fits the scenario.\n"
        "- A weaker reply should still sound plausible and human, but it should miss at least one high-value skill or allow one meaningful boundary drift.\n"
        "- The contrast between helpful and unhelpful mode should be obvious in empathy, boundary handling, and next-step support.\n"
        "- Silently check Agency, Relationship and Safety boundary management, emotional presence, and specificity before writing the final reply.\n"
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


def build_boundary_status_indicator(session) -> BoundaryStatusIndicator:
    from training.models import TurnAssessment
    from training.templatetags.assessment_ui import assessment_status
    latest = TurnAssessment.objects.filter(trainee_message__session=session).last()
    level = latest.overall_status_level if latest else 0
    status = assessment_status(level)
    return BoundaryStatusIndicator(score=level, tone=status['tone'], label=status['label'],
        headline=status['label'], summary=latest.feedback_text if latest else '', cue='')


def _roleplay_system_prompt(session, prompt, user_message: str, *, opening=False) -> str:
    persona = persona_prompt_for(session.scenario.boundary_type, session.persona)
    from training.models import TurnAssessment
    assessment = TurnAssessment.objects.filter(trainee_message__session=session).last()
    cue = assessment.roleplay_cue if assessment and not opening else ''
    if cue:
        persona += ('\nOptional context cue, only if consistent with the established persona: ' + cue
                    + '\nRespond naturally. Do not manufacture challenges or change established risk facts.')
    return "\n\n".join((
        prompt.context,
        persona,
        ROLEPLAY_SAFETY_CONSTRAINTS,
        ROLEPLAY_OPENING_INSTRUCTIONS if opening else ROLEPLAY_CONTINUATION_INSTRUCTIONS,
    ))


def _roleplay_opening_system_prompt(session, prompt) -> str:
    return _roleplay_system_prompt(session, prompt, "", opening=True)


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
        raise RuntimeError('Development test reply generation failed. No template was substituted.') from exc


def _generate_roleplay(session, user_message=None):
    from training.services.assessment import checked_reply, lock_session
    opening = user_message is None
    token = session.processing_token
    session.refresh_from_db()
    if session.status != 'IN_PROGRESS' or session.processing_token != token:
        raise PracticeFailure('SESSION_NOT_ACTIVE')
    if opening:
        existing = session.messages.filter(sender_type='AI').first()
        if existing:
            return existing
    provider = _provider()
    try:
        prompt = get_prompt_for_type('ROLE_PLAY', scenario=session.scenario)
        if provider == 'google':
            candidate = _google_role_play_opening(session, prompt) if opening else _google_role_play_reply(session, prompt, user_message)
        else:
            candidate = _openai_role_play_opening(session, prompt) if opening else _openai_role_play_reply(session, prompt, user_message)
        candidate = _strip_roleplay_framework_prefix(candidate)
        if not candidate or _roleplay_quality_issue(session, candidate, user_message or ''):
            raise ValueError('Invalid role-play output')
    except Exception as exc:
        session.processing_token = token
        pause(session, 'OPENING_GENERATION_FAILED' if opening else 'ROLEPLAY_GENERATION_FAILED')
        raise PracticeFailure(session.failure_category) from exc
    try:
        candidate, accepted = checked_reply(session, candidate)
        if not accepted:
            raise PracticeFailure('SAFETY_CHECK_FAILED')
    except PracticeFailure as exc:
        session.processing_token = token
        pause(session, exc.category)
        raise
    with transaction.atomic():
        lock_session(session)
        session.refresh_from_db()
        if session.status != 'IN_PROGRESS' or session.processing_token != token:
            raise PracticeFailure('SESSION_NOT_ACTIVE')
        if opening:
            existing = session.messages.filter(sender_type='AI').first()
            if existing:
                return existing
        message = Message.objects.create(session=session, sender_type='AI', content=candidate,
            response_source=Message.ResponseSources.GEMINI if provider == 'google' else Message.ResponseSources.OPENAI)
    return message


def generate_ai_opening(session):
    return _generate_roleplay(session)


def generate_ai_reply(session, user_message):
    return _generate_roleplay(session, user_message)


def generate_feedback(session):
    from training.services.assessment import saved_review
    return saved_review(session)


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


def get_prompt_for_type(prompt_type: str, scenario=None):
    return SimpleNamespace(
        **default_prompt_parts(prompt_type, scenario),
        framework_version=CMCV_FRAMEWORK_VERSION,
    )


def _contains_internal_roleplay_leak(text: str) -> bool:
    normalized = " ".join((text or "").lower().split())
    return any(marker in normalized for marker in ROLEPLAY_INTERNAL_LEAK_MARKERS)


def _contains_roleplay_coaching_leak(text: str) -> bool:
    """Reject dialogue that tells the trainee which assessed response to give."""
    compact = " ".join((text or "").split())
    return any(pattern.search(compact) for pattern in ROLEPLAY_COACHING_LEAK_PATTERNS)


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
            "uncertainty, or request without explaining the training exercise, assessing the trainee, "
            "or naming any boundary or training objective."
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
    return (
        "Write one stronger alternative reply that sounds more like a real help-seeker and less "
        "like a generic template."
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
            "Retrying Gemini role-play reply for session %s. finish_reason=%s issue=%s",
            session.id,
            finish_reason,
            quality_issue or "none",
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
