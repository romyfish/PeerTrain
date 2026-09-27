from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from training.models import Feedback
from training.services.ai import regrade_feedback_deterministically


class Command(BaseCommand):
    help = (
        "Regrade saved feedback with Boundary Skills Rubric 2.0 without "
        "calling an external model."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--confirm",
            action="store_true",
            help="Required acknowledgement that saved levels will be replaced.",
        )

    def handle(self, *args, **options):
        if not options["confirm"]:
            raise CommandError(
                "Refusing to regrade without --confirm. Back up the database first."
            )

        feedback_rows = Feedback.objects.select_related(
            "session",
            "session__scenario",
        ).order_by("id")
        updated = 0
        with transaction.atomic():
            for feedback in feedback_rows.iterator():
                regrade_feedback_deterministically(feedback)
                updated += 1

        self.stdout.write(
            self.style.SUCCESS(
                f"Regraded {updated} feedback record(s) without an LLM API call."
            )
        )
