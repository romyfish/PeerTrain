"""Read-only research records and explicit, audited researcher recovery."""
import uuid

from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.db.models import Count, Q, OuterRef, Subquery
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST, require_safe

from accounts.decorators import role_required
from training.models import TrainingSession, Message, LiveFeedbackEvent, SessionManagementEvent
from training.services.assessment import lock_session, reflect
from training.services.ai import generate_ai_opening, generate_feedback
from training.services.suspension import PracticeFailure, pause


@login_required
@role_required('ADMIN')
@require_safe
def prompt_inspection(request):
    from training.services.prompt_catalog import prompt_catalog
    from training.views import _app_context
    context = _app_context(request, 'admin_prompts', 'System prompts', is_admin=True)
    context.update(prompt_catalog(request.GET))
    return render(request, 'admin/prompts.html', context)


@login_required
@role_required('TRAINEE')
@require_POST
def live_feedback_event(request, session_id):
    session = get_object_or_404(TrainingSession, pk=session_id, user=request.user)
    kind = request.POST.get('event_type')
    if kind not in LiveFeedbackEvent.Types.values:
        return JsonResponse({'ok': False, 'error': 'Invalid feedback event.'}, status=400)
    # The client captures the visible turn at click time, even while a send is pending.
    try:
        turn = int(request.POST.get('trainee_turn_number', ''))
    except ValueError:
        return JsonResponse({'ok': False, 'error': 'Invalid turn.'}, status=400)
    users = session.messages.filter(sender_type='USER')
    if turn < 0 or turn > users.count():
        return JsonResponse({'ok': False, 'error': 'Invalid turn.'}, status=400)
    latest = users.order_by('id')[turn - 1] if turn else None
    event = LiveFeedbackEvent.objects.create(session=session, event_type=kind,
        trainee_turn_number=turn, latest_message=latest)
    return JsonResponse({'ok': True, 'id': event.pk, 'created_at': event.created_at.isoformat()})


@login_required
@role_required('ADMIN')
@require_safe
def records(request, user_id=None, logs=False):
    from training.views import _app_context
    latest_reply = Message.objects.filter(session_id=OuterRef('pk'), sender_type='AI').order_by('-id').values('id')[:1]
    sessions = TrainingSession.objects.select_related('user', 'scenario').annotate(last_reply_id=Subquery(latest_reply)).annotate(
        completed_turns=Count('messages', filter=Q(messages__sender_type='USER', messages__id__lt=Subquery(latest_reply)), distinct=True))
    selected = None
    if user_id is not None:
        selected = get_object_or_404(get_user_model(), pk=user_id)
        sessions = sessions.filter(user=selected)
    sessions = sessions.order_by('user__email', '-start_time', '-id')
    if logs:
        sessions = sessions.prefetch_related('interaction_logs').filter(interaction_logs__isnull=False).distinct()
    context = _app_context(request, 'admin_logs' if logs else 'admin_records',
        'Interaction logs' if logs else 'Practice records', is_admin=True)
    context.update(sessions=sessions, selected_user=selected, logs=logs)
    return render(request, 'admin/records.html', context)


@login_required
@role_required('ADMIN')
def record_detail(request, session_id):
    from training.views import _app_context, _complete_pending_turn
    session = get_object_or_404(TrainingSession.objects.select_related('user', 'scenario'), pk=session_id)
    if request.method == 'POST':
        action = request.POST.get('action')
        if request.POST.get('confirm') != str(session.pk):
            return JsonResponse({'ok': False, 'error': 'Explicit confirmation is required.'}, status=400)
        with transaction.atomic():
            lock_session(session)
            session.refresh_from_db()
            if action == 'pause_pending' and session.processing:
                # A worker may still be running. Final generation saves recheck status.
                if session.status == 'IN_PROGRESS':
                    pause(session, 'REQUEST_INTERRUPTED')
                else:
                    session.reflection_attention_at = timezone.now()
                    session.processing = False
                    session.processing_token = uuid.uuid4()
                    session.save(update_fields=['reflection_attention_at', 'processing', 'processing_token'])
            elif action == 'interrupt' and session.status == 'PAUSED':
                session.status = 'INTERRUPTED'
                session.end_time = timezone.now()
                session.processing = False
                session.save(update_fields=['status', 'end_time', 'processing'])
            elif action == 'resume' and session.status == 'PAUSED' and not session.processing:
                session.status = 'IN_PROGRESS'
                session.processing = True
                session.processing_token = uuid.uuid4()
                session.save(update_fields=['status', 'processing', 'processing_token'])
            elif action == 'retry_review' and session.status == 'COMPLETED' and session.review_attention_at and not session.processing:
                session.review_attention_at = None
                session.save(update_fields=['review_attention_at'])
            elif action == 'retry_reflection' and session.status == 'COMPLETED' and session.reflection_attention_at and not session.processing:
                session.reflection_attention_at = None
                session.processing = True
                session.processing_token = uuid.uuid4()
                session.save(update_fields=['reflection_attention_at', 'processing', 'processing_token'])
            else:
                return JsonResponse({'ok': False, 'error': 'This recovery action is not available.'}, status=409)
            SessionManagementEvent.objects.create(session=session, action=action.upper(),
                category=session.failure_category, actor=request.user)
        if action == 'resume':
            latest = session.messages.last()
            if latest and latest.sender_type == 'USER':
                _complete_pending_turn(session, latest)
            elif latest:
                TrainingSession.objects.filter(pk=session.pk).update(processing=False)
            else:
                try:
                    generate_ai_opening(session)
                    TrainingSession.objects.filter(pk=session.pk).update(processing=False)
                except Exception as exc:
                    pause(session, exc.category if isinstance(exc, PracticeFailure) else 'OPENING_GENERATION_FAILED')
        elif action == 'retry_review':
            generate_feedback(session)
        elif action == 'retry_reflection':
            pending = session.reflection_messages.filter(sender_type='USER').last()
            if pending:
                reflect(session, getattr(session, 'feedback', None), pending.content, pending.request_id, researcher_retry=True)
        return redirect('admin_record_detail', session_id=session.pk)
    if request.method not in {'GET', 'HEAD'}:
        return JsonResponse({'ok': False}, status=405)
    timeline = []
    for message in session.messages.select_related('assessment'):
        timeline.append({'at': message.created_at, 'kind': message.sender_type, 'message': message})
    for event in session.interaction_logs.all():
        timeline.append({'at': event.created_at, 'kind': 'Live Feedback', 'event': event})
    timeline.sort(key=lambda row: (row['at'], row['message'].pk if 'message' in row else row['event'].pk))
    context = _app_context(request, 'admin_records', 'Practice record', is_admin=True)
    context.update(session=session, timeline=timeline, feedback=getattr(session, 'feedback', None),
        reflection=getattr(session, 'reflection', None))
    return render(request, 'admin/record_detail.html', context)
