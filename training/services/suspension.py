"""Researcher-supervised failures. Categories contain no provider error text."""
import uuid
from django.utils import timezone
from training.models import TrainingSession, SessionManagementEvent

PAUSE_TITLE = 'Practice temporarily paused'
PAUSE_MESSAGE = "PeerTrain couldn't continue this session reliably. Your progress has been saved. Please contact the researcher before continuing."


class PracticeFailure(Exception):
    def __init__(self, category):
        self.category = category
        super().__init__(category)


def pause(session, category):
    now = timezone.now()
    changed = TrainingSession.objects.filter(pk=session.pk, status='IN_PROGRESS', processing_token=session.processing_token).update(
        status='PAUSED', failure_category=category, paused_at=now, processing=False, processing_token=uuid.uuid4())
    if changed:
        SessionManagementEvent.objects.create(session=session, action='PAUSE', category=category)
    session.refresh_from_db()


def attention(session, field, category):
    if TrainingSession.objects.filter(pk=session.pk, **{field: None}).update(**{field: timezone.now()}):
        SessionManagementEvent.objects.create(session=session, action='ATTENTION', category=category)
    session.refresh_from_db()
