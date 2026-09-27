import json

from django.core.management.base import BaseCommand, CommandError

from training.models import TrainingSession
from training.services.geval import run_geval_session


class Command(BaseCommand):
    help = (
        "Run the evidence-first G-Eval evaluator for one training session, "
        "persist its evidence and scores, and leave every row pending human review."
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
            result = run_geval_session(session, model=options.get("model"))
        except Exception as error:
            raise CommandError(str(error)) from error

        self.stdout.write(
            json.dumps(
                result.model_dump(mode="json"),
                ensure_ascii=False,
                indent=2,
            )
        )
        self.stdout.write(
            self.style.SUCCESS(
                f"Saved evaluation run {result.evaluation_run_id}; "
                "all turn rows are pending human review."
            )
        )
