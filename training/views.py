import uuid
import logging

from django.templatetags.static import static
from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.db.models import Count, Q
from django.http import Http404, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils import timezone

from accounts.decorators import role_required
from training.content import boundary_framework_for, scenario_map_by_boundary
from training.prompts import ROLEPLAY_SCENARIOS
from training.forms import ChatMessageForm
from training.models import FeedbackReviewItem, Message, PlatformSetting, Reflection, Scenario, TrainingSession, TurnAssessment
from training.services.ai import (build_boundary_status_indicator, generate_ai_opening,
    generate_ai_reply, generate_demo_trainee_reply, generate_feedback)
from training.services.assessment import assess_turn, ensure_reflection_opening, reflect, lock_session
from training.services.suspension import PracticeFailure, pause, PAUSE_MESSAGE, PAUSE_TITLE

logger = logging.getLogger(__name__)
from training.services.progress import (
    progress_snapshot,
    recalculate_user_progress,
)


def _test_reply_tools(user):
    return settings.DEBUG and user.is_test_account


def _sidebar_items(user=None, is_admin=False):
    if is_admin:
        return [
            {"label": "Users", "url": reverse("admin_users"), "name": "admin_users", "icon": "bi-people-fill"},
            {"label": "Practice records", "url": reverse("admin_records"), "name": "admin_records", "icon": "bi-journal-text"},
            {"label": "Interaction logs", "url": reverse("admin_logs"), "name": "admin_logs", "icon": "bi-clock-history"},
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


def _paused_result():
    return {"ok": False, "status": 409, "code": "session_paused", "error": PAUSE_TITLE + "\n" + PAUSE_MESSAGE}


def _complete_pending_turn(session, user_message):
    token = session.processing_token
    try:
        assess_turn(session, user_message)
        session.refresh_from_db()
        if session.processing_token != token or session.status != 'IN_PROGRESS':
            return _paused_result()
        reply = generate_ai_reply(session, user_message.content)
    except Exception as exc:
        session.processing_token = token
        pause(session, exc.category if isinstance(exc, PracticeFailure) else 'TURN_PROCESSING_FAILED')
        return _paused_result()
    TrainingSession.objects.filter(pk=session.pk, processing_token=token).update(processing=False)
    session.refresh_from_db()
    return {"ok": True, "user_message": user_message, "ai_message": reply, "notice": ""}


def _process_chat_turn(session, content, max_exchanges, *, user_response_source=Message.ResponseSources.USER_INPUT, request_id=None):
    if request_id:
        try:
            request_id = uuid.UUID(str(request_id))
        except ValueError:
            return {"ok": False, "status": 400, "code": "invalid_request", "error": "Invalid message request."}
    cleaned_content = (content or '').strip()
    if not cleaned_content:
        return {"ok": False, "status": 400, "code": "empty_message", "error": "Reply could not be empty."}
    # Commit the accepted message and claim before any external requests.
    with transaction.atomic():
        lock_session(session)
        session.refresh_from_db()
        if session.status == 'PAUSED':
            return _paused_result()
        if session.status != 'IN_PROGRESS':
            return {"ok": False, "status": 409, "code": "session_complete", "error": "This session has ended."}
        if request_id:
            if Message.objects.filter(request_id=request_id).exclude(session=session).exists():
                return {'ok': False, 'status': 400, 'code': 'invalid_request', 'error': 'Invalid message request.'}
            previous = session.messages.filter(request_id=request_id, sender_type='USER').first()
            if previous:
                reply = session.messages.filter(sender_type='AI', pk__gt=previous.pk).first()
                if reply:
                    return {"ok": True, "user_message": previous, "ai_message": reply, "notice": ""}
                return {"ok": False, "status": 409, "code": "turn_pending", "error": "This turn is pending. Please contact the researcher."}
        if session.processing:
            return {"ok": False, "status": 409, "code": "turn_pending", "error": "A request is already being processed."}
        latest = session.messages.last()
        if latest is None or latest.sender_type != 'AI':
            return {"ok": False, "status": 409, "code": "roleplay_not_started", "error": "Wait for the help-seeker before replying."}
        if session.messages.filter(sender_type='USER').count() >= max_exchanges:
            return {"ok": False, "status": 409, "code": "turn_limit", "error": "Maximum exchanges reached. Finish the session to review your feedback."}
        user_message = Message.objects.create(session=session, sender_type='USER', content=cleaned_content,
            response_source=user_response_source, request_id=request_id)
        session.processing = True
        session.processing_token = uuid.uuid4()
        session.save(update_fields=['processing', 'processing_token'])
    return _complete_pending_turn(session, user_message)


def _is_ajax_chat_request(request):
    return (
        request.headers.get("X-Requested-With") == "XMLHttpRequest"
        and "application/json" in request.headers.get("Accept", "")
    )


def _chat_live_state(session, max_exchanges):
    boundary_status = build_boundary_status_indicator(session)
    latest_assessment = session.messages.filter(sender_type="USER").order_by("-id").first()
    turn_assessments = list(TurnAssessment.objects.filter(trainee_message__session=session).select_related('trainee_message'))
    user_turns = session.messages.filter(sender_type=Message.SenderTypes.USER).count()
    return {
        "boundary_status": boundary_status,
        "turn_assessment": getattr(latest_assessment, "assessment", None),
        "turn_assessments": turn_assessments,
        "has_turn_feedback": any(assessment.feedback_text for assessment in turn_assessments),
        "user_turns": user_turns,
        "max_exchanges": max_exchanges,
        "can_send": (
            session.status == TrainingSession.Statuses.IN_PROGRESS
            and not session.processing
            and user_turns < max_exchanges
        ),
    }


def _chat_json_error(message, status, *, code="request_failed", can_send=True, session=None, request=None):
    return JsonResponse(
        {
            "ok": False,
            "code": code,
            "error": message,
            "can_send": can_send,
            "session_status": 'PAUSED' if code == 'session_paused' else None,
            "transcript_html": ''.join(render_to_string('trainee/includes/chat_message.html', {'message': m}, request=request) for m in session.messages.all()) if session is not None and code == 'session_paused' else '',
        },
        status=status,
    )


def _chat_opening_json_success(request, session, message, max_exchanges):
    state = _chat_live_state(session, max_exchanges)
    notice = ""
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
            "notice": "",
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
            {**state, "session": session},
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
    scenarios = list(Scenario.objects.filter(is_active=True, boundary_type__in=ROLEPLAY_SCENARIOS))
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
        scenario = get_object_or_404(Scenario, pk=request.POST.get("scenario_id"), is_active=True, boundary_type__in=ROLEPLAY_SCENARIOS)
        session = TrainingSession.objects.create(
            user=request.user,
            scenario=scenario,
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
        TrainingSession.objects.select_related("scenario", "user"),
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

    if session.scenario.boundary_type not in ROLEPLAY_SCENARIOS:
        raise Http404("This scenario is not available for practice.")

    settings_obj = PlatformSetting.get_solo()
    form = ChatMessageForm()
    scenario_meta = _scenario_meta_map().get(session.scenario.boundary_type, {})

    if request.method == "POST":
        session.refresh_from_db()
        if session.status == 'PAUSED':
            return _chat_json_error(PAUSE_TITLE + "\n" + PAUSE_MESSAGE, 409, code='session_paused', can_send=False, session=session, request=request)
        if session.status != 'IN_PROGRESS':
            return _chat_json_error('This session has ended.', 409, code='session_complete', can_send=False)
        action = request.POST.get("action", "send")
        if session.processing:
            return _chat_json_error('A request is already being processed. Please contact the researcher if it remains pending.', 409, code='turn_pending', can_send=False)
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
            with transaction.atomic():
                lock_session(session)
                session.refresh_from_db()
                if session.status != 'IN_PROGRESS' or session.processing:
                    return _chat_json_error('This session cannot advance.', 409, can_send=False)
                latest = session.messages.last()
                if latest and latest.sender_type == 'USER':
                    return _chat_json_error('A turn needs researcher attention.', 409, can_send=False)
                session.status = TrainingSession.Statuses.COMPLETED
                session.end_time = timezone.now()
                session.ended_early = user_turns < 4
                session.save(update_fields=['status', 'end_time', 'ended_early'])
            return redirect("session_review", session_id=session.id)

        if action == "select_persona":
            persona = request.POST.get("persona", "pso")
            if persona not in {"pso", "student"}:
                return _chat_json_error("Choose a valid persona.", 400)
            if not session.messages.exists():
                TrainingSession.objects.filter(pk=session.pk, persona="").update(persona=persona)
            session.refresh_from_db()
            state = _chat_live_state(session, settings_obj.max_exchanges_per_session)
            return JsonResponse({
                "ok": True, "name": session.character["name"],
                "portrait": static(session.character["portrait"]),
                "boundary_coach_html": render_to_string('trainee/includes/boundary_coach_live.html',
                    {**state, 'session': session}, request=request),
            })

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

            TrainingSession.objects.filter(pk=session.pk, persona="").update(persona="pso")
            session.refresh_from_db()
            if not TrainingSession.objects.filter(pk=session.pk, status='IN_PROGRESS', processing=False).update(processing=True, processing_token=uuid.uuid4()):
                return _chat_json_error('A request is already being processed.', 409, code='turn_pending', can_send=False)
            session.refresh_from_db()
            try:
                opening_message = generate_ai_opening(session)
            except Exception as exc:
                pause(session, exc.category if isinstance(exc, PracticeFailure) else 'OPENING_GENERATION_FAILED')
                if is_ajax:
                    return _chat_json_error(PAUSE_TITLE + "\n" + PAUSE_MESSAGE, 409, code='session_paused', can_send=False, session=session, request=request)
                return redirect('chat', session_id=session.pk)
            TrainingSession.objects.filter(pk=session.pk, processing_token=session.processing_token).update(processing=False)
            session.refresh_from_db()

            if is_ajax:
                return _chat_opening_json_success(
                    request,
                    session,
                    opening_message,
                    settings_obj.max_exchanges_per_session,
                )
            return redirect("chat", session_id=session.id)

        if action == "prepare_auto_reply":
            if not _test_reply_tools(request.user):
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
                    "Development test reply generation failed. Please contact the researcher.",
                    500,
                    code="test_reply_failed",
                )
            return _chat_prepared_reply_json(prepared_reply)

        if action in {"auto_helpful", "auto_unhelpful"}:
            if not _test_reply_tools(request.user):
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
            try:
                auto_reply = generate_demo_trainee_reply(session, quality)
            except Exception:
                return _chat_json_error('Development test reply generation failed. Please contact the researcher.', 503, code='test_reply_failed')
            result = _process_chat_turn(
                session,
                auto_reply.content,
                settings_obj.max_exchanges_per_session,
                user_response_source=auto_reply.response_source,
                request_id=request.POST.get('request_id') or None,
            )
            if not result["ok"]:
                if is_ajax:
                    return _chat_json_error(
                        result["error"],
                        result["status"],
                        code=result.get("code", "request_failed"),
                        can_send=result["status"] != 409, session=session, request=request,
                    )
                messages.warning(request, result["error"])
                return redirect("chat", session_id=session.id)

            auto_notice = ""
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
            }
            if _test_reply_tools(request.user) and generated_source in allowed_generated_sources:
                user_response_source = generated_source
            result = _process_chat_turn(
                session,
                form.cleaned_data["content"],
                settings_obj.max_exchanges_per_session,
                user_response_source=user_response_source,
                request_id=request.POST.get('request_id') or None,
            )
            if not result["ok"]:
                if is_ajax:
                    return _chat_json_error(
                        result["error"],
                        result["status"],
                        code=result.get("code", "request_failed"),
                        can_send=result["status"] != 409, session=session, request=request,
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
    if session.status != 'IN_PROGRESS' or session.processing:
        form.fields['content'].widget.attrs['disabled'] = True
    needs_roleplay_opening = session.status == 'IN_PROGRESS' and not session.processing and not session.messages.exists()
    context = _app_context(request, "chat", "Role-play Chat")
    context.update(
        {
            "session": session,
            "chat_messages": session.messages.all(),
            "form": form,
            "scenario_meta": scenario_meta,
            "show_test_reply_tools": _test_reply_tools(request.user),
            "practice_pending": session.processing,
            "practice_paused": session.status == "PAUSED",
            "needs_roleplay_opening": needs_roleplay_opening,
            "needs_persona_selection": needs_roleplay_opening and not session.persona,
            "roleplay_scenario": ROLEPLAY_SCENARIOS[session.scenario.boundary_type],
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
    ensure_reflection_opening(session)
    if request.method == "POST":
        action = request.POST.get("action", "save_notes")
        if action == "reflect":
            content = request.POST.get("content", "").strip()
            try:
                request_id = uuid.UUID(request.POST.get("request_id", ""))
            except ValueError:
                return JsonResponse({"ok": False, "error": "Invalid reflection request."}, status=400)
            if not content or len(content) > 12000:
                return JsonResponse({"ok": False, "error": "Enter a reflection of up to 12,000 characters."}, status=400)
            try:
                if not reflect(session, feedback, content, request_id):
                    return JsonResponse({"ok": False, "code": "reflection_attention", "error": "Reflection AI is unavailable. Your message is saved; please contact the researcher before continuing.",
                        "messages": list(session.reflection_messages.values('sender_type', 'content'))}, status=503)
            except Exception:
                logger.warning("Reflection unavailable for session %s", session.pk)
                return JsonResponse({"ok": False, "error": "Reflection AI is unavailable. Please contact the researcher before continuing."}, status=503)
            return JsonResponse({"ok": True, "messages": list(session.reflection_messages.values("sender_type", "content"))})
        if action != "save_notes":
            return JsonResponse({"ok": False, "error": "Unknown review action."}, status=400)
        Reflection.objects.update_or_create(session=session, defaults={"reflection_text": request.POST.get("reflection_text", "")})
        recalculate_user_progress(request.user)
        if _is_ajax_chat_request(request):
            return JsonResponse({"ok": True, "confirmation": "Saved"})
        messages.success(request, "Saved")
        return redirect("session_review", session_id=session.id)
    examples = list(feedback.review_items.all()) if feedback else []
    timeline = []
    for message in session.messages.select_related("session__scenario", "assessment"):
        assessment = getattr(message, "assessment", None)
        turn_examples = [item for item in examples if item.message_id == message.pk]
        # Some historical reviews have a saved turn alternative but no review item.
        # Render that existing text without creating or changing any saved records.
        record = assessment.record if assessment else {}
        alternative = record.get("alternative_reply")
        if alternative and not any(item.better_reply == alternative for item in turn_examples):
            turn_examples.append({"message_id": message.pk, "better_reply": alternative,
                                  "why_it_matters": record.get("alternative_reason", "")})
        timeline.append({"message": message, "assessment": assessment, "examples": turn_examples})
    context = _app_context(request, "history", "Session Review")
    context.update(session=session, feedback=feedback, timeline=timeline, examples=examples,
        reflection=getattr(session, "reflection", None),
        reflection_messages=session.reflection_messages.all(), reflection_request_id=uuid.uuid4(),
        scenario_meta=_scenario_meta_map().get(session.scenario.boundary_type, {}))
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
        session.history_title = session.scenario.display_title
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
    get_object_or_404(TrainingSession, pk=session_id, user=request.user, status=TrainingSession.Statuses.COMPLETED)
    return redirect("session_review", session_id=session_id)


@login_required
@role_required("TRAINEE")
def progress_view(request):
    return redirect("dashboard")


@login_required
@role_required("ADMIN")
def admin_dashboard_view(request):
    return admin_users_view(request)


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
            if request.POST.get('confirm_delete') != str(user.pk):
                messages.error(request, 'Explicit deletion confirmation is required.')
                return redirect('admin_users')
            if user.training_sessions.exists():
                messages.error(request, 'This account has research records. Deactivate it to preserve those records.')
                return redirect('admin_users')
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
