import json
from uuid import uuid4

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from accounts.models import User
from training.models import (
    BoundaryEvaluationRun,
    Message,
    Prompt,
    Scenario,
    TrainingSession,
)
from training.services.geval import ensure_reliable_geval_run


class Command(BaseCommand):
    help = (
        "Run a production-path G-Eval smoke test using synthetic conversation "
        "content. All temporary database records are rolled back."
    )

    def handle(self, *args, **options):
        result = None
        try:
            with transaction.atomic():
                suffix = uuid4().hex[:12]
                user = User.objects.create_user(
                    username=f"geval-smoke-{suffix}",
                    email=f"geval-smoke-{suffix}@example.invalid",
                    password=uuid4().hex,
                    role="TRAINEE",
                )
                scenario = Scenario.objects.create(
                    title=f"Synthetic G-Eval Smoke {suffix}",
                    description="Synthetic deployment validation only.",
                    boundary_type=Scenario.BoundaryTypes.AGENCY,
                    estimated_duration_min=5,
                    learning_objectives="Preserve help-seeker choice.",
                    is_active=False,
                )
                prompt = Prompt.objects.create(
                    name=f"Synthetic G-Eval Smoke {suffix}",
                    type=Prompt.PromptTypes.ROLE_PLAY,
                    scenario=scenario,
                    method="Synthetic deployment validation only.",
                )
                session = TrainingSession.objects.create(
                    user=user,
                    scenario=scenario,
                    prompt=prompt,
                )
                Message.objects.create(
                    session=session,
                    sender_type=Message.SenderTypes.AI,
                    content=(
                        "I cannot decide whether to stay on my course or change. "
                        "Please tell me what to do."
                    ),
                )
                Message.objects.create(
                    session=session,
                    sender_type=Message.SenderTypes.USER,
                    content=(
                        "That sounds like a difficult decision. What matters most "
                        "to you about each option?"
                    ),
                )
                run = ensure_reliable_geval_run(
                    session,
                    purpose=BoundaryEvaluationRun.Purposes.LIVE,
                    force=True,
                )
                result = {
                    "status": run.status,
                    "resolution_status": run.resolution_status,
                    "production_ready": run.production_ready,
                    "scores": run.resolved_scores,
                    "resolution_details": run.resolution_details,
                    "verification_passes": run.verification_runs.count(),
                    "synthetic_data_persisted": False,
                }
                transaction.set_rollback(True)
        except Exception as error:
            raise CommandError(str(error)) from error

        self.stdout.write(json.dumps(result, ensure_ascii=False, indent=2))
        self.stdout.write(
            self.style.SUCCESS(
                "Synthetic production-path smoke test completed; database writes rolled back."
            )
        )
