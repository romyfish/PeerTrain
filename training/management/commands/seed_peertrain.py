from training.prompts import ROLEPLAY_SCENARIOS

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand

from training.content import (
    SCENARIO_LIBRARY,
)
from training.models import PlatformSetting, Scenario


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
            scenario.description = ROLEPLAY_SCENARIOS[boundary_type]["guidance"]
            scenario.estimated_duration_min = item["estimated_duration_min"]
            scenario.learning_objectives = "\n".join(item["learning_objectives"])
            scenario.is_active = True
            scenario.save()
            scenario_lookup[boundary_type] = scenario

        # Preserve historical relationships while keeping only the three active scenarios selectable.
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

        teo, _ = user_model.objects.update_or_create(
            email="teo.wang@university.ac.uk",
            defaults={
                "username": "teo.wang@university.ac.uk",
                "first_name": "Teo",
                "last_name": "Wang",
                "role": user_model.Roles.TRAINEE,
                "is_active": True,
            },
        )
        teo.set_password("password123")
        teo.save()

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

        self.stdout.write(self.style.SUCCESS("PeerTrain scenarios, accounts and settings refreshed; existing records preserved."))
