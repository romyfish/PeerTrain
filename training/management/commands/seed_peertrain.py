from training.prompts import opening_message_for
from datetime import datetime, timedelta

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand
from django.utils import timezone

from training.content import (
    SCENARIO_LIBRARY,
)
from training.models import (
    Feedback,
    FeedbackScore,
    Message,
    PlatformSetting,
    Reflection,
    Scenario,
    TrainingSession,
)
from training.services.progress import recalculate_user_progress
from training.services.boundary_scoring import RUBRIC_VERSION, conservative_median_score


class Command(BaseCommand):
    help = "Seed PeerTrain with demo scenarios, settings, and sample users."

    def handle(self, *args, **options):
        user_model = get_user_model()
        settings_obj = PlatformSetting.get_solo()
        settings_obj.max_exchanges_per_session = 20
        settings_obj.save()

        legacy_titles = {
            "AGENCY": ("Emotional Boundary", "Relational Focus Boundary"),
            "INTEGRATED": (
                "Privacy Boundary",
                "Privacy & Confidentiality Boundary",
                "Integrated Boundary Practice",
            ),
            "SAFETY": (
                "Referral Boundary",
                "Role Scope & Escalation Boundary",
                "Safety & Escalation Boundary",
            ),
            "RELATIONSHIP": (
                "Burnout & Compassion Fatigue",
                "Time & Availability Boundary",
                "Supporter Relationship Boundary",
            ),
        }
        scenario_lookup = {}
        for item in SCENARIO_LIBRARY:
            boundary_type = item["boundary_type"]
            scenario = (
                Scenario.objects.filter(
                    boundary_type=boundary_type,
                    title__in=[item["title"], *legacy_titles[boundary_type]],
                )
                .order_by("id")
                .first()
            )
            if scenario is None:
                scenario = Scenario(boundary_type=boundary_type)
            scenario.title = item["title"]
            scenario.description = item["description"]
            scenario.estimated_duration_min = item["estimated_duration_min"]
            scenario.learning_objectives = "\n".join(item["learning_objectives"])
            scenario.is_active = True
            scenario.save()
            scenario_lookup[boundary_type] = scenario

        # Preserve historical relationships while keeping only the four canonical scenarios selectable.
        Scenario.objects.exclude(id__in=[scenario.id for scenario in scenario_lookup.values()]).update(
            is_active=False
        )

        trainee, _ = user_model.objects.update_or_create(
            email="alex.chen@university.ac.uk",
            defaults={
                "username": "alex.chen@university.ac.uk",
                "first_name": "Alex",
                "last_name": "Chen",
                "role": user_model.Roles.TRAINEE,
                "is_active": True,
            },
        )
        trainee.set_password("password123")
        trainee.save()

        admin_user, _ = user_model.objects.update_or_create(
            email="admin@university.ac.uk",
            defaults={
                "username": "admin@university.ac.uk",
                "first_name": "Sarah",
                "last_name": "Kim",
                "role": user_model.Roles.ADMIN,
                "is_active": True,
                "is_staff": True,
            },
        )
        admin_user.set_password("password123")
        admin_user.save()

        sessions_to_keep = []
        base_scores = [
            ("AGENCY", 2, 2, None),
            ("RELATIONSHIP", 2, 3, None),
            ("SAFETY", 3, 3, 2),
            ("INTEGRATED", 3, 3, 3),
            ("AGENCY", 4, 3, None),
            ("RELATIONSHIP", 4, 4, None),
            ("SAFETY", 4, 4, 4),
            ("INTEGRATED", 4, 4, 5),
        ]
        anchor = datetime(2026, 1, 15, 10, 0, tzinfo=timezone.get_current_timezone())

        for index, (boundary_type, agency, relationship, safety) in enumerate(base_scores):
            scenario = scenario_lookup[boundary_type]
            observed_scores = [
                score for score in (agency, relationship, safety) if score is not None
            ]
            overall = conservative_median_score(observed_scores)
            start_time = anchor - timedelta(days=14 - index * 2)
            session = (
                TrainingSession.objects.filter(
                    user=trainee,
                    scenario=scenario,
                    overall_score=overall,
                )
                .order_by("id")
                .first()
            )
            if session is None:
                session = TrainingSession(
                    user=trainee,
                    scenario=scenario,
                    overall_score=overall,
                )
            session.end_time = start_time + timedelta(minutes=18)
            session.status = TrainingSession.Statuses.COMPLETED
            session.rubric_version = RUBRIC_VERSION
            session.save()
            TrainingSession.objects.filter(pk=session.pk).update(
                start_time=start_time,
                end_time=start_time + timedelta(minutes=18),
            )
            session.refresh_from_db()
            sessions_to_keep.append(session.id)
            Message.objects.update_or_create(
                session=session,
                sender_type=Message.SenderTypes.AI,
                created_at=session.start_time,
                defaults={"content": opening_message_for(boundary_type, session.id)},
            )
            Message.objects.update_or_create(
                session=session,
                sender_type=Message.SenderTypes.USER,
                created_at=session.start_time + timedelta(minutes=2),
                defaults={"content": "Thank you for telling me. I want to understand what feels most difficult right now."},
            )
            Message.objects.update_or_create(
                session=session,
                sender_type=Message.SenderTypes.AI,
                created_at=session.start_time + timedelta(minutes=4),
                defaults={"content": "I think the part that scares me most is that it keeps getting bigger in my head."},
            )

            feedback, _ = Feedback.objects.update_or_create(
                session=session,
                defaults={
                    "overall_score": overall,
                    "strengths": "Kept the help-seeker involved in the next step\nNamed one boundary without withdrawing support",
                    "improvements": "State the practical limit more explicitly\nOffer one collaborative next action",
                    "rubric_version": RUBRIC_VERSION,
                },
            )
            dimension_scores = (
                (FeedbackScore.Criteria.AGENCY, agency),
                (FeedbackScore.Criteria.RELATIONSHIP, relationship),
                (FeedbackScore.Criteria.SAFETY, safety),
            )
            for criterion, score in dimension_scores:
                FeedbackScore.objects.update_or_create(
                    feedback=feedback,
                    criterion=criterion,
                    defaults={
                        "score": score,
                        "observed": score is not None,
                        "evidence": "The trainee response provides a concrete boundary behaviour.",
                        "rationale": "Rated against the Boundary Skills Rubric behavioural anchor.",
                        "next_move": "Make the limit and collaborative next step explicit.",
                        "rubric_version": RUBRIC_VERSION,
                    },
                )
            feedback.scores.exclude(
                criterion__in=[
                    FeedbackScore.Criteria.AGENCY,
                    FeedbackScore.Criteria.RELATIONSHIP,
                    FeedbackScore.Criteria.SAFETY,
                ]
            ).delete()
            Reflection.objects.update_or_create(
                session=session,
                defaults={
                    "reflection_text": (
                        "I noticed that the response needed a clearer boundary while keeping "
                        "the help-seeker involved."
                    ),
                    "action_plan": "Next time I will state one limit and offer one shared next step.",
                },
            )

        TrainingSession.objects.filter(user=trainee).exclude(id__in=sessions_to_keep).delete()
        recalculate_user_progress(trainee)
        self.stdout.write(self.style.SUCCESS("PeerTrain seed data created or refreshed."))
