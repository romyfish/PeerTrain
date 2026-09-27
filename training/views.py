import logging

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db.models import Avg, Count, F, Max, Q
from django.http import Http404, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils import timezone

from accounts.decorators import role_required
from training.content import (
    SCENARIO_LIBRARY,
    boundary_framework_for,
    scenario_map_by_boundary,
)
from training.forms import (
    BoundaryTurnReviewForm,
    ChatMessageForm,
    PlatformSettingForm,
    PromptForm,
    ReflectionForm,
)
from training.models import (
    BoundaryEvaluationRun,
    BoundaryTurnEvaluation,
    Feedback,
    FeedbackReviewItem,
    FeedbackScore,
    Message,
    PlatformSetting,
    Prompt,
    Reflection,
    Scenario,
    TrainingSession,
)
from training.services.ai import (
    build_boundary_map_indicator,
    build_boundary_status_indicator,
    final_feedback_assessments,
    generate_ai_opening,
    generate_ai_reply,
    generate_demo_trainee_reply,
    generate_feedback,
    generate_reflection_guidance,
    get_prompt_for_type,
)
from training.services.boundary_scoring import RUBRIC_VERSION
from training.services.geval import ensure_reliable_geval_run
from training.services.review_queue import (
    apply_human_review_resolution,
    grouped_review_queue,
)

logger = logging.getLogger(__name__)
from training.services.progress import (
    admin_analytics_snapshot,
    progress_snapshot,
    recalculate_user_progress,
)


def _boundary_score_steps(active_score):
    return [
        {
            "score": 1,
            "label": "At risk",
            "tone": "risk",
            "icon_asset": "images/boundary-status/level-1-at-risk.svg",
            "active": active_score == 1,
        },
        {
            "score": 2,
            "label": "Wobbling",
            "tone": "wobbling",
            "icon_asset": "images/boundary-status/level-2-wobbling.svg",
            "active": active_score == 2,
        },
        {
            "score": 3,
            "label": "Mixed",
            "tone": "mixed",
            "icon_asset": "images/boundary-status/level-3-mixed.svg",
            "active": active_score == 3,
        },
        {
            "score": 4,
            "label": "Mostly steady",
            "tone": "mostly-steady",
            "icon_asset": "images/boundary-status/level-4-mostly-steady.svg",
            "active": active_score == 4,
        },
        {
            "score": 5,
            "label": "Very steady",
            "tone": "very-steady",
            "icon_asset": "images/boundary-status/level-5-very-steady.svg",
            "active": active_score == 5,
        },
    ]


def _sidebar_items(user=None, is_admin=False):
    if is_admin:
        return [
            {"label": "Dashboard", "url": reverse("admin_dashboard"), "name": "admin_dashboard", "icon": "bi-grid-1x2-fill"},
            {"label": "Users", "url": reverse("admin_users"), "name": "admin_users", "icon": "bi-people-fill"},
            {"label": "Prompts", "url": reverse("admin_prompts"), "name": "admin_prompts", "icon": "bi-chat-left-text-fill"},
            {"label": "Score reviews", "url": reverse("admin_scoring_reviews"), "name": "admin_scoring_reviews", "icon": "bi-clipboard2-check-fill"},
            {"label": "Analytics", "url": reverse("admin_analytics"), "name": "admin_analytics", "icon": "bi-graph-up-arrow"},
            {"label": "Settings", "url": reverse("admin_settings"), "name": "admin_settings", "icon": "bi-sliders2"},
        ]

    return [
        {"label": "Home", "url": reverse("dashboard"), "name": "dashboard", "icon": "bi-house-door-fill"},
        {"label": "Scenarios", "url": reverse("scenarios"), "name": "scenarios", "icon": "bi-book-fill"},
        {"label": "History", "url": reverse("history"), "name": "history", "icon": "bi-clock-history"},
    ]


def _app_context(request, current_page, title, is_admin=False):
    return {
        "page_title": title,
        "current_page": current_page,
        "sidebar_items": _sidebar_items(user=request.user, is_admin=is_admin),
        "sidebar_theme": "admin" if is_admin else "trainee",
    }


def _scenario_meta_map():
    return scenario_map_by_boundary()


def _active_prompt(prompt_type, scenario=None):
    return get_prompt_for_type(prompt_type, scenario=scenario)


def _process_chat_turn(
    session,
    content,
    max_exchanges,
    *,
    user_response_source=Message.ResponseSources.USER_INPUT,
):
    cleaned_content = (content or "").strip()
    if not cleaned_content:
        return {
            "ok": False,
            "status": 400,
            "code": "empty_message",
            "error": "Reply could not be empty.",
        }

    current_user_turns = session.messages.filter(
        sender_type=Message.SenderTypes.USER,
    ).count()
    if current_user_turns >= max_exchanges:
        return {
            "ok": False,
            "status": 409,
            "code": "turn_limit",
            "error": (
                "Maximum exchanges reached for this prototype session. "
                "Finish the session to review your feedback."
            ),
        }

    user_message = Message.objects.create(
        session=session,
        sender_type=Message.SenderTypes.USER,
        content=cleaned_content,
        response_source=user_response_source,
    )
    ai_message = generate_ai_reply(session, cleaned_content)
    try:
        ensure_reliable_geval_run(
            session,
            purpose=BoundaryEvaluationRun.Purposes.LIVE,
        )
    except Exception:
        logger.exception(
            "The reliable boundary judge failed for session %s; "
            "the deterministic score remains active.",
            session.id,
        )
    notice = ""
    if getattr(ai_message, "used_fallback", False):
        notice = getattr(
            ai_message,
            "fallback_notice",
            "This turn used a placeholder help-seeker reply because the LLM request failed.",
        )

    return {
        "ok": True,
        "user_message": user_message,
        "ai_message": ai_message,
        "notice": notice,
    }


def _is_ajax_chat_request(request):
    return (
        request.headers.get("X-Requested-With") == "XMLHttpRequest"
        and "application/json" in request.headers.get("Accept", "")
    )


def _chat_live_state(session, max_exchanges):
    boundary_status = build_boundary_status_indicator(session)
    boundary_map = build_boundary_map_indicator(session)
    user_turns = session.messages.filter(sender_type=Message.SenderTypes.USER).count()
    return {
        "boundary_status": boundary_status,
        "boundary_map": boundary_map,
        "boundary_scale_steps": _boundary_score_steps(boundary_status.score),
        "user_turns": user_turns,
        "max_exchanges": max_exchanges,
        "can_send": (
            session.status == TrainingSession.Statuses.IN_PROGRESS
            and user_turns < max_exchanges
        ),
    }


def _chat_json_error(message, status, *, code="request_failed", can_send=True):
    return JsonResponse(
        {
            "ok": False,
            "code": code,
            "error": message,
            "can_send": can_send,
        },
        status=status,
    )


def _chat_opening_json_success(request, session, message, max_exchanges):
    state = _chat_live_state(session, max_exchanges)
    notice = ""
    if getattr(message, "used_fallback", False):
        notice = getattr(
            message,
            "fallback_notice",
            "This opening used a backup reply because the LLM request failed.",
        )
    try:
        opening_message_html = render_to_string(
            "trainee/includes/chat_message.html",
            {"message": message},
            request=request,
        )
    except Exception:
        logger.exception("Could not render the opening message for session %s.", session.id)
        return _chat_json_error(
            "The opening was saved but could not be displayed. Reload the conversation.",
            500,
            code="response_render_failed",
        )
    return JsonResponse(
        {
            "ok": True,
            "opening_message_html": opening_message_html,
            "notice": notice,
            "response_source": message.response_source,
            "can_send": state["can_send"],
        }
    )


def _chat_prepared_reply_json(reply, *, can_send=True):
    return JsonResponse(
        {
            "ok": True,
            "generated_content": reply.content,
            "response_source": reply.response_source,
            "notice": reply.fallback_notice if reply.used_fallback else "",
            "can_send": can_send,
        }
    )


def _chat_json_success(request, session, result, max_exchanges, notice=""):
    state = _chat_live_state(session, max_exchanges)
    combined_notice = " ".join(
        part.strip()
        for part in [notice, result.get("notice", "")]
        if part and part.strip()
    )
    try:
        user_message_html = render_to_string(
            "trainee/includes/chat_message.html",
            {"message": result["user_message"]},
            request=request,
        )
        ai_message_html = render_to_string(
            "trainee/includes/chat_message.html",
            {"message": result["ai_message"]},
            request=request,
        )
        boundary_coach_html = render_to_string(
            "trainee/includes/boundary_coach_live.html",
            state,
            request=request,
        )
    except Exception:
        logger.exception("Could not render the AJAX chat response for session %s.", session.id)
        return _chat_json_error(
            "The messages were saved but could not be displayed. Reload the conversation before retrying.",
            500,
            code="response_render_failed",
            can_send=state["can_send"],
        )
    return JsonResponse(
        {
            "ok": True,
            "user_message_html": user_message_html,
            "ai_message_html": ai_message_html,
            "boundary_coach_html": boundary_coach_html,
            "user_turns": state["user_turns"],
            "max_exchanges": state["max_exchanges"],
            "boundary_score": state["boundary_status"].score,
            "boundary_label": state["boundary_status"].label,
            "notice": combined_notice,
            "can_send": state["can_send"],
        }
    )


@login_required
@role_required("TRAINEE")
def dashboard_view(request):
    snapshot = progress_snapshot(request.user)
    context = _app_context(request, "dashboard", "Home")
    context.update(
        {
            "snapshot": snapshot,
            "skill_trends_json": snapshot["skill_trends"],
            "skill_breakdown_json": snapshot["skill_breakdown"],
        }
    )
    return render(request, "trainee/dashboard.html", context)


@login_required
@role_required("TRAINEE")
def scenarios_view(request):
    scenarios = list(Scenario.objects.filter(is_active=True))
    meta_map = _scenario_meta_map()
    scenario_order = {
        boundary_type: meta.get("order", 99)
        for boundary_type, meta in meta_map.items()
    }
    scenarios.sort(
        key=lambda scenario: scenario_order.get(
            scenario.boundary_type,
            len(scenario_order),
        )
    )

    if request.method == "POST":
        scenario = get_object_or_404(Scenario, pk=request.POST.get("scenario_id"), is_active=True)
        role_play_prompt = _active_prompt(Prompt.PromptTypes.ROLE_PLAY, scenario=scenario)
        session = TrainingSession.objects.create(
            user=request.user,
            scenario=scenario,
            prompt=role_play_prompt,
        )
        # 先进入聊天页，再由页面异步请求 AI 开场，避免模型延迟阻塞页面跳转。
        return redirect("chat", session_id=session.id)

    cards = [
        {
            "scenario": scenario,
            "meta": meta_map.get(scenario.boundary_type, {}),
        }
        for scenario in scenarios
    ]
    context = _app_context(request, "scenarios", "Scenario Selection")
    context.update({"cards": cards})
    return render(request, "trainee/scenarios.html", context)


@login_required
@role_required("TRAINEE")
def chat_view(request, session_id):
    is_ajax = _is_ajax_chat_request(request)
    session = get_object_or_404(
        TrainingSession.objects.select_related("scenario", "prompt", "user"),
        pk=session_id,
        user=request.user,
    )
    if session.status == TrainingSession.Statuses.COMPLETED:
        if request.method == "POST" and is_ajax:
            return _chat_json_error(
                "This session has already been completed.",
                409,
                code="session_complete",
                can_send=False,
            )
        return redirect("session_review", session_id=session.id)

    settings_obj = PlatformSetting.get_solo()
    form = ChatMessageForm()
    scenario_meta = _scenario_meta_map().get(session.scenario.boundary_type, {})

    if request.method == "POST":
        action = request.POST.get("action", "send")
        if action == "finish":
            user_turns = session.messages.filter(
                sender_type=Message.SenderTypes.USER,
            ).count()
            if user_turns < 1:
                error = "Reply at least once before ending this session."
                if is_ajax:
                    return _chat_json_error(
                        error,
                        409,
                        code="finish_too_early",
                    )
                messages.warning(request, error)
                return redirect("chat", session_id=session.id)
            session.status = TrainingSession.Statuses.COMPLETED
            session.end_time = timezone.now()
            session.ended_early = user_turns < 4
            session.rubric_version = RUBRIC_VERSION
            session.save(
                update_fields=[
                    "status",
                    "end_time",
                    "ended_early",
                    "rubric_version",
                ]
            )
            return redirect("session_review", session_id=session.id)

        if action == "start_roleplay":
            existing_ai_message = (
                session.messages.filter(sender_type=Message.SenderTypes.AI)
                .order_by("created_at", "id")
                .first()
            )
            if existing_ai_message:
                if is_ajax:
                    return _chat_opening_json_success(
                        request,
                        session,
                        existing_ai_message,
                        settings_obj.max_exchanges_per_session,
                    )
                return redirect("chat", session_id=session.id)

            if session.messages.exists():
                error = "This conversation has already started and cannot create a new opening."
                if is_ajax:
                    return _chat_json_error(
                        error,
                        409,
                        code="opening_conflict",
                    )
                messages.warning(request, error)
                return redirect("chat", session_id=session.id)

            try:
                opening_message = generate_ai_opening(session)
            except Exception:
                logger.exception("Could not generate the role-play opening for session %s.", session.id)
                error = "The AI help-seeker could not start the conversation. Please try again."
                if is_ajax:
                    return _chat_json_error(
                        error,
                        500,
                        code="opening_failed",
                    )
                messages.error(request, error)
                return redirect("chat", session_id=session.id)

            if is_ajax:
                return _chat_opening_json_success(
                    request,
                    session,
                    opening_message,
                    settings_obj.max_exchanges_per_session,
                )
            if getattr(opening_message, "fallback_notice", ""):
                messages.warning(request, opening_message.fallback_notice)
            return redirect("chat", session_id=session.id)

        if action == "prepare_auto_reply":
            if not request.user.is_test_account:
                return _chat_json_error(
                    "Quick test replies are only available for the Alex Chen demo account.",
                    403,
                    code="test_reply_forbidden",
                )
            if not is_ajax:
                messages.warning(request, "Quick test replies require JavaScript.")
                return redirect("chat", session_id=session.id)
            if not session.messages.filter(sender_type=Message.SenderTypes.AI).exists():
                return _chat_json_error(
                    "Wait for the AI help-seeker to open the conversation first.",
                    409,
                    code="roleplay_not_started",
                )

            existing_user_turns = session.messages.filter(
                sender_type=Message.SenderTypes.USER,
            ).count()
            if existing_user_turns >= settings_obj.max_exchanges_per_session:
                return _chat_json_error(
                    "Maximum exchanges reached. Finish the session to review your feedback.",
                    409,
                    code="turn_limit",
                    can_send=False,
                )

            quality = request.POST.get("quality", "")
            if quality not in {"helpful", "unhelpful"}:
                return _chat_json_error(
                    "Choose either a better or a poor test reply.",
                    400,
                    code="invalid_reply_quality",
                )
            try:
                prepared_reply = generate_demo_trainee_reply(session, quality)
            except Exception:
                logger.exception("Could not prepare a test reply for session %s.", session.id)
                return _chat_json_error(
                    "The test reply could not be generated. Please try again.",
                    500,
                    code="test_reply_failed",
                )
            return _chat_prepared_reply_json(prepared_reply)

        if action in {"auto_helpful", "auto_unhelpful"}:
            if not request.user.is_test_account:
                error = "Quick test replies are only available for the Alex Chen demo account."
                if is_ajax:
                    return _chat_json_error(error, 403, code="test_reply_forbidden")
                messages.warning(
                    request,
                    error,
                )
                return redirect("chat", session_id=session.id)

            existing_user_turns = session.messages.filter(
                sender_type=Message.SenderTypes.USER,
            ).count()
            if existing_user_turns >= settings_obj.max_exchanges_per_session:
                error = (
                    "Maximum exchanges reached for this prototype session. "
                    "Finish the session to review your feedback."
                )
                if is_ajax:
                    return _chat_json_error(
                        error,
                        409,
                        code="turn_limit",
                        can_send=False,
                    )
                messages.warning(request, error)
                return redirect("chat", session_id=session.id)

            quality = "helpful" if action == "auto_helpful" else "unhelpful"
            auto_reply = generate_demo_trainee_reply(session, quality)
            result = _process_chat_turn(
                session,
                auto_reply.content,
                settings_obj.max_exchanges_per_session,
                user_response_source=auto_reply.response_source,
            )
            if not result["ok"]:
                if is_ajax:
                    return _chat_json_error(
                        result["error"],
                        result["status"],
                        code=result.get("code", "request_failed"),
                        can_send=result["status"] != 409,
                    )
                messages.warning(request, result["error"])
                return redirect("chat", session_id=session.id)

            auto_notice = auto_reply.fallback_notice if auto_reply.used_fallback else ""
            if is_ajax:
                return _chat_json_success(
                    request,
                    session,
                    result,
                    settings_obj.max_exchanges_per_session,
                    notice=auto_notice,
                )
            for notice in [auto_notice, result.get("notice", "")]:
                if notice:
                    messages.warning(request, notice)
            return redirect("chat", session_id=session.id)

        form = ChatMessageForm(request.POST)
        if form.is_valid():
            user_response_source = Message.ResponseSources.USER_INPUT
            generated_source = request.POST.get("generated_response_source", "")
            allowed_generated_sources = {
                Message.ResponseSources.OPENAI,
                Message.ResponseSources.GEMINI,
                Message.ResponseSources.PLACEHOLDER,
            }
            if request.user.is_test_account and generated_source in allowed_generated_sources:
                user_response_source = generated_source
            result = _process_chat_turn(
                session,
                form.cleaned_data["content"],
                settings_obj.max_exchanges_per_session,
                user_response_source=user_response_source,
            )
            if not result["ok"]:
                if is_ajax:
                    return _chat_json_error(
                        result["error"],
                        result["status"],
                        code=result.get("code", "request_failed"),
                        can_send=result["status"] != 409,
                    )
                messages.warning(request, result["error"])
                return redirect("chat", session_id=session.id)
            if is_ajax:
                return _chat_json_success(
                    request,
                    session,
                    result,
                    settings_obj.max_exchanges_per_session,
                )
            if result.get("notice"):
                messages.warning(request, result["notice"])
            return redirect("chat", session_id=session.id)
        if is_ajax:
            error = form.errors.get("content", ["Reply could not be empty."])[0]
            return _chat_json_error(str(error), 400, code="invalid_message")

    live_state = _chat_live_state(session, settings_obj.max_exchanges_per_session)
    needs_roleplay_opening = not session.messages.exists()
    context = _app_context(request, "chat", "Role-play Chat")
    context.update(
        {
            "session": session,
            "chat_messages": session.messages.all(),
            "form": form,
            "scenario_meta": scenario_meta,
            "show_test_reply_tools": request.user.is_test_account,
            "needs_roleplay_opening": needs_roleplay_opening,
            **live_state,
        }
    )
    return render(request, "trainee/chat.html", context)


@login_required
@role_required("TRAINEE")
def feedback_view(request, session_id):
    return redirect("session_review", session_id=session_id)


@login_required
@role_required("TRAINEE")
def reflection_view(request, session_id):
    return redirect("session_review", session_id=session_id)


@login_required
@role_required("TRAINEE")
def session_review_view(request, session_id):
    session = get_object_or_404(
        TrainingSession.objects.select_related("scenario", "user"),
        pk=session_id,
        user=request.user,
    )
    if session.status != TrainingSession.Statuses.COMPLETED:
        return redirect("chat", session_id=session.id)
    feedback = generate_feedback(session)
    reflection_guidance = generate_reflection_guidance(session, feedback)
    reflection_prompt = _active_prompt(
        Prompt.PromptTypes.REFLECTION,
        session.scenario,
    )
    reflection = getattr(session, "reflection", None)
    if request.method == "POST":
        form = ReflectionForm(request.POST, instance=reflection)
        if form.is_valid():
            reflection = form.save(commit=False)
            reflection.session = session
            reflection.prompt = reflection_prompt
            reflection.save()
            recalculate_user_progress(request.user)
            messages.success(request, "Reflection saved to your history.")
            return redirect("history_detail", session_id=session.id)
    else:
        form = ReflectionForm(instance=reflection)

    score_map = {score.criterion: score for score in feedback.scores.all()}
    assessment_map = final_feedback_assessments(session)
    scenario_meta = _scenario_meta_map().get(session.scenario.boundary_type, {})
    score_definitions = (
        (
            FeedbackScore.Criteria.AGENCY,
            "Agency Boundary",
            "bi-compass",
            "agency",
        ),
        (
            FeedbackScore.Criteria.RELATIONSHIP,
            "Relationship Boundary",
            "bi-person-bounding-box",
            "relationship",
        ),
        (
            FeedbackScore.Criteria.SAFETY,
            "Safety Boundary",
            "bi-signpost-split",
            "safety",
        ),
    )
    context = _app_context(request, "history", "Session Review")
    context.update(
        {
            "session": session,
            "feedback": feedback,
            "form": form,
            "reflection": reflection,
            "reflection_guidance": reflection_guidance,
            "scenario_meta": scenario_meta,
            "score_cards": [
                {
                    "criterion": criterion,
                    "label": label,
                    "icon": icon,
                    "tone": tone,
                    "score": score_map.get(criterion),
                    "assessment": assessment_map.get(tone),
                }
                for criterion, label, icon, tone in score_definitions
            ],
        }
    )
    return render(request, "trainee/session_review.html", context)


@login_required
@role_required("TRAINEE")
def history_view(request):
    all_sessions = list(
        TrainingSession.objects.filter(
            user=request.user,
            status=TrainingSession.Statuses.COMPLETED,
        )
        .select_related("scenario")
        .prefetch_related("feedback__review_items", "feedback__scores")
        .order_by("-end_time", "-start_time", "-id")
    )
    dimension_to_boundary_key = {
        "agency": FeedbackReviewItem.BoundaryKeys.AGENCY,
        "relationship": FeedbackReviewItem.BoundaryKeys.RELATIONSHIP,
        "safety": FeedbackReviewItem.BoundaryKeys.SAFETY,
    }
    for session in all_sessions:
        session.history_title = boundary_framework_for(
            session.scenario.boundary_type
        ).get("canonical_name", session.scenario.title)
        boundary_keys = set()
        dimension_key = boundary_framework_for(
            session.scenario.boundary_type
        ).get("dimension_key")
        if dimension_key in dimension_to_boundary_key:
            boundary_keys.add(dimension_to_boundary_key[dimension_key])
        if hasattr(session, "feedback"):
            boundary_keys.update(
                item.boundary_key for item in session.feedback.review_items.all()
            )
        session.history_boundary_keys = boundary_keys

    valid_boundaries = {
        key for key, _label in FeedbackReviewItem.BoundaryKeys.choices
    }
    selected_boundary = request.GET.get("boundary", "").upper()
    if selected_boundary not in valid_boundaries:
        selected_boundary = ""
    sessions = [
        session
        for session in all_sessions
        if not selected_boundary
        or selected_boundary in session.history_boundary_keys
    ]

    boundary_filters = []
    for key, label in FeedbackReviewItem.BoundaryKeys.choices:
        boundary_filters.append(
            {
                "key": key,
                "label": label,
                "count": sum(
                    key in session.history_boundary_keys for session in all_sessions
                ),
                "active": selected_boundary == key,
            }
        )
    selected_label = next(
        (
            label
            for key, label in FeedbackReviewItem.BoundaryKeys.choices
            if key == selected_boundary
        ),
        "All sessions",
    )
    context = _app_context(request, "history", "History")
    context.update(
        {
            "sessions": sessions,
            "all_session_count": len(all_sessions),
            "boundary_filters": boundary_filters,
            "selected_boundary": selected_boundary,
            "selected_label": selected_label,
        }
    )
    return render(request, "trainee/history.html", context)


@login_required
@role_required("TRAINEE")
def history_detail_view(request, session_id):
    session = get_object_or_404(
        TrainingSession.objects.select_related(
            "scenario",
            "feedback",
            "reflection",
        ).prefetch_related(
            "messages",
            "feedback__scores",
            "feedback__review_items",
        ),
        pk=session_id,
        user=request.user,
        status=TrainingSession.Statuses.COMPLETED,
    )
    if not hasattr(session, "feedback"):
        generate_feedback(session)
        session.refresh_from_db()
    context = _app_context(request, "history", "History Entry")
    scenario_meta = _scenario_meta_map().get(session.scenario.boundary_type, {})
    context.update(
        {
            "session": session,
            "feedback": session.feedback,
            "reflection": getattr(session, "reflection", None),
            "scenario_meta": scenario_meta,
        }
    )
    return render(request, "trainee/history_detail.html", context)


@login_required
@role_required("TRAINEE")
def progress_view(request):
    return redirect("dashboard")


@login_required
@role_required("ADMIN")
def admin_dashboard_view(request):
    review_rows = BoundaryTurnEvaluation.objects.exclude(
        review_scope=BoundaryTurnEvaluation.ReviewScopes.NONE
    )
    stats = {
        "users": request.user.__class__.objects.count(),
        "trainees": request.user.__class__.objects.filter(role="TRAINEE").count(),
        "in_progress_sessions": TrainingSession.objects.filter(
            status=TrainingSession.Statuses.IN_PROGRESS
        ).count(),
        "active_prompts": Prompt.objects.filter(is_active=True).count(),
        "completed_sessions": TrainingSession.objects.filter(status="COMPLETED").count(),
        "avg_score": round(
            Feedback.objects.aggregate(avg=Avg("overall_score"))["avg"] or 0,
            1,
        ),
        "required_score_reviews": review_rows.filter(
            review_scope=BoundaryTurnEvaluation.ReviewScopes.REQUIRED,
            review_status=BoundaryTurnEvaluation.ReviewStatuses.PENDING,
        )
        .values("run_id", "boundary_key")
        .distinct()
        .count(),
        "sampled_score_reviews": review_rows.filter(
            review_scope=BoundaryTurnEvaluation.ReviewScopes.QUALITY_SAMPLE,
            review_status=BoundaryTurnEvaluation.ReviewStatuses.PENDING,
        )
        .values("run_id", "boundary_key")
        .distinct()
        .count(),
    }
    recent_sessions = (
        TrainingSession.objects.filter(status="COMPLETED")
        .select_related("user", "scenario")
        .order_by("-start_time")[:6]
    )
    context = _app_context(request, "admin_dashboard", "Admin Dashboard", is_admin=True)
    context.update(
        {
            "stats": stats,
            "recent_sessions": recent_sessions,
        }
    )
    return render(request, "admin/dashboard.html", context)


@login_required
@role_required("ADMIN")
def admin_scoring_reviews_view(request):
    evaluations = (
        BoundaryTurnEvaluation.objects.exclude(
            review_scope=BoundaryTurnEvaluation.ReviewScopes.NONE
        )
        .select_related(
            "run",
            "run__session",
            "run__session__user",
            "run__session__scenario",
            "reviewed_by",
        )
        .order_by("-run__created_at", "-run_id", "boundary_key", "trainee_turn_number")
    )

    boundary = request.GET.get("boundary", "").strip().upper()
    if boundary in BoundaryTurnEvaluation.BoundaryKeys.values:
        evaluations = evaluations.filter(boundary_key=boundary)
    else:
        boundary = ""

    scope = request.GET.get("scope", "").strip().upper()
    if scope in {
        BoundaryTurnEvaluation.ReviewScopes.REQUIRED,
        BoundaryTurnEvaluation.ReviewScopes.QUALITY_SAMPLE,
    }:
        evaluations = evaluations.filter(review_scope=scope)
    else:
        scope = ""

    reason = request.GET.get("reason", "").strip().upper()
    if reason in BoundaryTurnEvaluation.ReviewReasons.values:
        evaluations = evaluations.filter(review_reason=reason)
    else:
        reason = ""

    query = request.GET.get("q", "").strip()
    if query:
        evaluations = evaluations.filter(
            Q(run__session__user__email__icontains=query)
            | Q(run__session__user__first_name__icontains=query)
            | Q(run__session__user__last_name__icontains=query)
            | Q(run__session__scenario__title__icontains=query)
            | Q(evidence_interpretation__icontains=query)
        )

    rows = grouped_review_queue(evaluations)
    status = request.GET.get("status", "pending").strip().lower()
    if status == "reviewed":
        rows = [row for row in rows if not row["is_pending"]]
    elif status == "all":
        pass
    else:
        status = "pending"
        rows = [row for row in rows if row["is_pending"]]

    all_selected = BoundaryTurnEvaluation.objects.exclude(
        review_scope=BoundaryTurnEvaluation.ReviewScopes.NONE
    )
    pending_required = (
        all_selected.filter(
            review_scope=BoundaryTurnEvaluation.ReviewScopes.REQUIRED,
            review_status=BoundaryTurnEvaluation.ReviewStatuses.PENDING,
        )
        .values("run_id", "boundary_key")
        .distinct()
        .count()
    )
    pending_samples = (
        all_selected.filter(
            review_scope=BoundaryTurnEvaluation.ReviewScopes.QUALITY_SAMPLE,
            review_status=BoundaryTurnEvaluation.ReviewStatuses.PENDING,
        )
        .values("run_id", "boundary_key")
        .distinct()
        .count()
    )
    total_groups = (
        all_selected.values("run_id", "boundary_key").distinct().count()
    )
    pending_groups = (
        all_selected.filter(
            review_status=BoundaryTurnEvaluation.ReviewStatuses.PENDING
        )
        .values("run_id", "boundary_key")
        .distinct()
        .count()
    )

    paginator = Paginator(rows, 20)
    page_obj = paginator.get_page(request.GET.get("page"))
    query_params = request.GET.copy()
    query_params.pop("page", None)
    context = _app_context(
        request,
        "admin_scoring_reviews",
        "Score Review Queue",
        is_admin=True,
    )
    context.update(
        {
            "page_obj": page_obj,
            "status_filter": status,
            "scope_filter": scope,
            "boundary_filter": boundary,
            "reason_filter": reason,
            "search_query": query,
            "boundary_choices": BoundaryTurnEvaluation.BoundaryKeys.choices,
            "reason_choices": BoundaryTurnEvaluation.ReviewReasons.choices,
            "queue_stats": {
                "required": pending_required,
                "samples": pending_samples,
                "reviewed": max(total_groups - pending_groups, 0),
                "total": total_groups,
            },
            "review_sample_percent": round(
                settings.BOUNDARY_JUDGE_REVIEW_SAMPLE_RATE * 100,
                1,
            ),
            "query_without_page": query_params.urlencode(),
        }
    )
    return render(request, "admin/scoring_reviews.html", context)


@login_required
@role_required("ADMIN")
def admin_scoring_review_detail_view(request, run_id, boundary_key):
    boundary_key = boundary_key.upper()
    if boundary_key not in BoundaryTurnEvaluation.BoundaryKeys.values:
        raise Http404
    run = get_object_or_404(
        BoundaryEvaluationRun.objects.select_related(
            "session",
            "session__user",
            "session__scenario",
        ),
        pk=run_id,
    )
    evaluations = list(
        run.turn_evaluations.filter(boundary_key=boundary_key)
        .exclude(review_scope=BoundaryTurnEvaluation.ReviewScopes.NONE)
        .select_related(
            "trainee_message",
            "help_seeker_message",
            "reviewed_by",
        )
        .order_by("trainee_turn_number", "id")
    )
    if not evaluations:
        raise Http404

    invalid_form = None
    invalid_evaluation_id = None
    if request.method == "POST":
        action = request.POST.get("action")
        if action == "confirm_all":
            pending = [
                evaluation
                for evaluation in evaluations
                if evaluation.review_status
                == BoundaryTurnEvaluation.ReviewStatuses.PENDING
            ]
            for evaluation in pending:
                evaluation.review_status = (
                    BoundaryTurnEvaluation.ReviewStatuses.CONFIRMED
                )
                evaluation.corrected_score = None
                evaluation.corrected_judgement = ""
                evaluation.reviewed_by = request.user
                evaluation.reviewed_at = timezone.now()
                evaluation.save(
                    update_fields=[
                        "review_status",
                        "corrected_score",
                        "corrected_judgement",
                        "reviewed_by",
                        "reviewed_at",
                    ]
                )
            apply_human_review_resolution(run, boundary_key)
            messages.success(
                request,
                f"Confirmed {len(pending)} pending judgement"
                f"{'' if len(pending) == 1 else 's'}.",
            )
            return redirect(
                "admin_scoring_review_detail",
                run_id=run.id,
                boundary_key=boundary_key,
            )

        evaluation_id = request.POST.get("evaluation_id", "")
        evaluation = next(
            (
                item
                for item in evaluations
                if str(item.id) == evaluation_id
            ),
            None,
        )
        if evaluation is None:
            raise Http404
        invalid_evaluation_id = evaluation.id
        invalid_form = BoundaryTurnReviewForm(
            request.POST,
            instance=evaluation,
            prefix=f"review-{evaluation.id}",
        )
        if invalid_form.is_valid():
            reviewed = invalid_form.save(commit=False)
            reviewed.reviewed_by = request.user
            reviewed.reviewed_at = timezone.now()
            reviewed.save()
            apply_human_review_resolution(run, boundary_key)
            messages.success(
                request,
                f"Saved the review for turn {reviewed.trainee_turn_number}.",
            )
            return redirect(
                "admin_scoring_review_detail",
                run_id=run.id,
                boundary_key=boundary_key,
            )

    review_rows = []
    for evaluation in evaluations:
        form = (
            invalid_form
            if invalid_evaluation_id == evaluation.id
            else BoundaryTurnReviewForm(
                instance=evaluation,
                prefix=f"review-{evaluation.id}",
            )
        )
        review_rows.append({"evaluation": evaluation, "form": form})

    group = grouped_review_queue(
        run.turn_evaluations.filter(boundary_key=boundary_key)
        .exclude(review_scope=BoundaryTurnEvaluation.ReviewScopes.NONE)
        .select_related("run")
    )[0]
    context = _app_context(
        request,
        "admin_scoring_reviews",
        f"{group['boundary_label']} Review",
        is_admin=True,
    )
    context.update(
        {
            "run": run,
            "group": group,
            "review_rows": review_rows,
            "pending_count": sum(
                item.review_status
                == BoundaryTurnEvaluation.ReviewStatuses.PENDING
                for item in evaluations
            ),
        }
    )
    return render(request, "admin/scoring_review_detail.html", context)


@login_required
@role_required("ADMIN")
def admin_users_view(request):
    user_model = request.user.__class__
    if request.method == "POST":
        action = request.POST.get("action")
        user = get_object_or_404(user_model, pk=request.POST.get("user_id"))
        if user == request.user:
            messages.warning(request, "You cannot modify the account you are currently using.")
            return redirect("admin_users")

        if action == "toggle_active":
            user.is_active = not user.is_active
            user.save(update_fields=["is_active"])
            messages.success(request, f"Updated active status for {user.full_name_or_email}.")
            return redirect("admin_users")

        if action == "delete_user":
            deleted_label = user.full_name_or_email
            user.delete()
            messages.success(request, f"Deleted user {deleted_label}.")
        return redirect("admin_users")

    users = (
        user_model.objects.annotate(
            completed_session_count=Count(
                "training_sessions",
                filter=Q(
                    training_sessions__status=TrainingSession.Statuses.COMPLETED,
                ),
                distinct=True,
            )
        )
        .order_by("role", "first_name", "last_name", "email")
    )
    context = _app_context(request, "admin_users", "Admin User Management", is_admin=True)
    context.update(
        {
            "users": users,
            "user_summary": {
                "total": users.count(),
                "admins": users.filter(role="ADMIN").count(),
                "trainees": users.filter(role="TRAINEE").count(),
                "active": users.filter(is_active=True).count(),
            },
        }
    )
    return render(request, "admin/users.html", context)


PROMPT_GROUPS = (
    {
        "key": "default",
        "title": "Global fallback prompts",
        "tab_label": "Global fallbacks",
        "description": "Global fallbacks used when a scenario-specific prompt is unavailable.",
        "prompt_type": None,
    },
    {
        "key": "role_play",
        "title": "Role-play prompts",
        "tab_label": "Role-play",
        "description": "Control how the AI help-seeker behaves in each training scenario.",
        "prompt_type": Prompt.PromptTypes.ROLE_PLAY,
    },
    {
        "key": "feedback",
        "title": "Feedback prompts",
        "tab_label": "Feedback",
        "description": "Shape structured scoring and post-conversation feedback.",
        "prompt_type": Prompt.PromptTypes.FEEDBACK,
    },
    {
        "key": "reflection",
        "title": "Reflection prompts",
        "tab_label": "Reflection",
        "description": "Guide the trainee's post-session reflective practice.",
        "prompt_type": Prompt.PromptTypes.REFLECTION,
    },
)

PROTECTED_PROMPT_CONTRACTS = {
    Prompt.PromptTypes.ROLE_PLAY: (
        "The model must remain the first-person AI help-seeker, preserve its role state, "
        "respond to the latest trainee message, and output only natural dialogue. Role "
        "switching, scores, coaching language, metadata, and repeated wording are rejected."
    ),
    Prompt.PromptTypes.FEEDBACK: (
        "The Boundary Skills Rubric fixes the Agency, Relationship, and Safety order and 1-5 anchors. "
        "Each trainee reply is evaluated only against the move made relevant by the preceding "
        "help-seeker message. Session feedback accumulates genuine opportunities and later repairs; "
        "it never requires all three process moves in every reply. Observed scores require evidence "
        "verified against a real trainee message."
    ),
    Prompt.PromptTypes.REFLECTION: (
        "The output must contain exactly two personalised questions grounded in verified "
        "session evidence. It must not write the trainee's answer or grade reflection quality."
    ),
}


def _prompt_group_key(prompt):
    if prompt.scenario_id is None:
        return "default"
    return {
        Prompt.PromptTypes.ROLE_PLAY: "role_play",
        Prompt.PromptTypes.FEEDBACK: "feedback",
        Prompt.PromptTypes.REFLECTION: "reflection",
    }[prompt.type]


def _admin_prompts_group_url(group_key):
    return f"{reverse('admin_prompts')}?group={group_key}"


def _visible_prompt_or_404(queryset, raw_prompt_id):
    try:
        prompt_id = int(raw_prompt_id)
    except (TypeError, ValueError) as error:
        raise Http404("Prompt not found.") from error
    return get_object_or_404(queryset, pk=prompt_id)


@login_required
@role_required("ADMIN")
def admin_prompts_view(request):
    visible_prompts = Prompt.objects.filter(
        Q(scenario__isnull=True) | Q(scenario__is_active=True)
    ).filter(is_archived=False).select_related("scenario")
    valid_group_keys = {group["key"] for group in PROMPT_GROUPS}
    requested_group = (
        request.POST.get("group")
        if request.method == "POST"
        else request.GET.get("group")
    )
    active_group_key = (
        requested_group if requested_group in valid_group_keys else "default"
    )
    edit_instance = None
    form = None
    drawer_mode = None

    if request.method == "GET" and request.GET.get("edit"):
        edit_instance = _visible_prompt_or_404(visible_prompts, request.GET["edit"])
        active_group_key = _prompt_group_key(edit_instance)
        form = PromptForm(instance=edit_instance)
        drawer_mode = "edit"
    elif request.method == "GET" and request.GET.get("create") == "1":
        initial_type = next(
            (
                group["prompt_type"]
                for group in PROMPT_GROUPS
                if group["key"] == active_group_key
            ),
            None,
        )
        form = PromptForm(initial={"type": initial_type} if initial_type else None)
        drawer_mode = "create"

    if request.method == "POST":
        action = request.POST.get("action")
        if action == "toggle_active":
            prompt = _visible_prompt_or_404(
                visible_prompts,
                request.POST.get("prompt_id"),
            )
            prompt.is_active = not prompt.is_active
            prompt.save()
            messages.success(request, f"Updated {prompt.name}.")
            return redirect(_admin_prompts_group_url(_prompt_group_key(prompt)))

        if request.POST.get("prompt_id"):
            edit_instance = _visible_prompt_or_404(
                visible_prompts,
                request.POST["prompt_id"],
            )
        form = PromptForm(request.POST, instance=edit_instance)
        drawer_mode = "edit" if edit_instance else "create"
        if form.is_valid():
            saved_prompt = form.save()
            messages.success(request, "Prompt saved successfully.")
            return redirect(
                _admin_prompts_group_url(_prompt_group_key(saved_prompt))
            )

    prompts = list(visible_prompts)
    scenario_order = {
        row["boundary_type"]: index for index, row in enumerate(SCENARIO_LIBRARY)
    }
    prompts.sort(
        key=lambda prompt: (
            prompt.type,
            -1 if prompt.scenario_id is None else scenario_order.get(
                prompt.scenario.boundary_type,
                len(scenario_order),
            ),
            prompt.name,
        )
    )
    grouped_prompts = {group["key"]: [] for group in PROMPT_GROUPS}
    for prompt in prompts:
        grouped_prompts[_prompt_group_key(prompt)].append(prompt)

    prompt_groups = []
    for definition in PROMPT_GROUPS:
        group = dict(definition)
        group["prompts"] = grouped_prompts[group["key"]]
        group["count"] = len(group["prompts"])
        group["is_active"] = group["key"] == active_group_key
        prompt_groups.append(group)
    active_group = next(group for group in prompt_groups if group["is_active"])
    selected_prompt_type = None
    if form:
        selected_prompt_type = (
            form.data.get("type")
            if form.is_bound
            else form.initial.get("type")
            or getattr(form.instance, "type", None)
        )

    context = _app_context(request, "admin_prompts", "Admin Prompt Management", is_admin=True)
    context.update(
        {
            "form": form,
            "prompt_groups": prompt_groups,
            "active_group": active_group,
            "drawer_mode": drawer_mode,
            "editing": edit_instance,
            "framework_version": Prompt.FRAMEWORK_VERSION,
            "protected_contract": PROTECTED_PROMPT_CONTRACTS.get(
                selected_prompt_type,
                "Select a prompt type to view its protected runtime contract.",
            ),
            "protected_contracts": PROTECTED_PROMPT_CONTRACTS,
            "drawer_return_selector": (
                f"#prompt-edit-{edit_instance.id}"
                if edit_instance
                else "#prompt-new-button"
            ),
        }
    )
    return render(request, "admin/prompts.html", context)


@login_required
@role_required("ADMIN")
def admin_analytics_view(request):
    user_model = request.user.__class__
    trainee_choices = (
        user_model.objects.filter(
            role="TRAINEE",
            training_sessions__isnull=False,
        )
        .distinct()
        .order_by("first_name", "last_name", "email")
    )
    selected_trainee = None
    selected_id = request.GET.get("trainee", "").strip()
    if selected_id.isdigit():
        selected_trainee = trainee_choices.filter(id=int(selected_id)).first()

    snapshot = admin_analytics_snapshot(selected_trainee=selected_trainee)
    trainee_rows = (
        user_model.objects.filter(role="TRAINEE")
        .annotate(
            completed_session_count=Count(
                "training_sessions",
                filter=Q(
                    training_sessions__status=TrainingSession.Statuses.COMPLETED,
                ),
                distinct=True,
            ),
            observed_session_count=Count(
                "training_sessions",
                filter=Q(
                    training_sessions__status=TrainingSession.Statuses.COMPLETED,
                    training_sessions__feedback__scores__observed=True,
                    training_sessions__feedback__scores__score__isnull=False,
                    training_sessions__feedback__scores__rubric_version=RUBRIC_VERSION,
                    training_sessions__feedback__scores__criterion__in=[
                        FeedbackScore.Criteria.AGENCY,
                        FeedbackScore.Criteria.RELATIONSHIP,
                        FeedbackScore.Criteria.SAFETY,
                    ],
                ),
                distinct=True,
            ),
            average_score=Avg(
                "training_sessions__feedback__scores__score",
                filter=Q(
                    training_sessions__status=TrainingSession.Statuses.COMPLETED,
                    training_sessions__feedback__scores__observed=True,
                    training_sessions__feedback__scores__score__isnull=False,
                    training_sessions__feedback__scores__rubric_version=RUBRIC_VERSION,
                    training_sessions__feedback__scores__criterion__in=[
                        FeedbackScore.Criteria.AGENCY,
                        FeedbackScore.Criteria.RELATIONSHIP,
                        FeedbackScore.Criteria.SAFETY,
                    ],
                ),
            ),
            last_activity=Max(
                "training_sessions__end_time",
                filter=Q(
                    training_sessions__status=TrainingSession.Statuses.COMPLETED,
                ),
            ),
        )
        .order_by(
            F("last_activity").desc(nulls_last=True),
            "first_name",
            "last_name",
            "email",
        )
    )
    context = _app_context(request, "admin_analytics", "Admin Analytics", is_admin=True)
    context.update(
        {
            "snapshot": snapshot,
            "selected_trainee": selected_trainee,
            "trainee_choices": trainee_choices,
            "trainee_rows": trainee_rows,
        }
    )
    return render(request, "admin/analytics.html", context)


@login_required
@role_required("ADMIN")
def admin_settings_view(request):
    settings_obj = PlatformSetting.get_solo()
    if request.method == "POST":
        form = PlatformSettingForm(request.POST, instance=settings_obj)
        if form.is_valid():
            form.save()
            messages.success(request, "Platform settings saved successfully.")
            return redirect("admin_settings")
    else:
        form = PlatformSettingForm(instance=settings_obj)

    context = _app_context(request, "admin_settings", "Admin Settings", is_admin=True)
    context.update({"form": form})
    return render(request, "admin/settings.html", context)
