import json

from django.core.management.base import BaseCommand, CommandError

from training.models import TrainingSession
from training.services.geval import preview_geval_session


class Command(BaseCommand):
    help = (
        "Run a non-persisting G-Eval preview for one training session and print "
        "the structured result for human review."
    )

    def add_arguments(self, parser):
        parser.add_argument("--session-id", type=int, required=True)
        parser.add_argument(
            "--model",
            help="Optional OpenAI model override. Defaults to OPENAI_MODEL.",
        )

    def handle(self, *args, **options):
        try:
            session = TrainingSession.objects.select_related("scenario").get(
                pk=options["session_id"]
            )
        except TrainingSession.DoesNotExist as error:
            raise CommandError("Training session not found.") from error

        try:
            preview = preview_geval_session(session, model=options.get("model"))
        except Exception as error:
            raise CommandError(str(error)) from error

        self.stdout.write(
            json.dumps(
                preview.model_dump(mode="json"),
                ensure_ascii=False,
                indent=2,
            )
        )
        self.stdout.write(
            self.style.WARNING(
                "Review only: this G-Eval preview was not written to the database."
            )
        )
