import json
import sqlite3
from datetime import datetime
from pathlib import Path

from django.conf import settings
from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from training.models import (
    Feedback,
    FeedbackReviewItem,
    FeedbackScore,
    Message,
    Reflection,
    TrainingSession,
)


class Command(BaseCommand):
    help = (
        "Back up the SQLite database, remove all training history, and rebuild "
        "the Alex Chen Boundary Skills Rubric demonstration history."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--confirm",
            action="store_true",
            help="Required acknowledgement that all users' training history will be deleted.",
        )
        parser.add_argument(
            "--skip-demo",
            action="store_true",
            help="Do not rebuild the Alex Chen demonstration history after clearing.",
        )

    def handle(self, *args, **options):
        if not options["confirm"]:
            raise CommandError(
                "Refusing to clear training history without --confirm. "
                "Back up production before running this command."
            )

        database_path = Path(settings.DATABASES["default"]["NAME"]).resolve()
        if not database_path.exists():
            raise CommandError(f"SQLite database not found: {database_path}")

        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        backup_dir = Path(settings.BASE_DIR) / "backups"
        backup_dir.mkdir(parents=True, exist_ok=True)
        backup_path = backup_dir / f"peertrain-before-boundary-rubric-{timestamp}.sqlite3"
        summary_path = backup_dir / f"peertrain-before-boundary-rubric-{timestamp}.json"

        summary = {
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "database": str(database_path),
            "counts": {
                "training_sessions": TrainingSession.objects.count(),
                "messages": Message.objects.count(),
                "feedback": Feedback.objects.count(),
                "feedback_scores": FeedbackScore.objects.count(),
                "feedback_review_items": FeedbackReviewItem.objects.count(),
                "reflections": Reflection.objects.count(),
            },
        }
        source_connection = sqlite3.connect(database_path)
        backup_connection = sqlite3.connect(backup_path)
        try:
            source_connection.backup(backup_connection)
        finally:
            backup_connection.close()
            source_connection.close()
        summary_path.write_text(
            json.dumps(summary, indent=2),
            encoding="utf-8",
        )

        with transaction.atomic():
            deleted_count, _ = TrainingSession.objects.all().delete()

        if not options["skip_demo"]:
            call_command("seed_peertrain")

        self.stdout.write(
            self.style.SUCCESS(
                f"Cleared {deleted_count} training-related records. "
                f"Database backup: {backup_path}"
            )
        )
        self.stdout.write(f"Pre-clear summary: {summary_path}")
