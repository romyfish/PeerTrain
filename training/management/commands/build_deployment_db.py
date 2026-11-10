"""Build a fresh deployment database without changing the source database."""

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from datetime import date, datetime, time

from django.apps import apps
from django.conf import settings
from django.core import serializers
from django.core.serializers.json import DjangoJSONEncoder
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from accounts.models import User
from training.models import TrainingSession


class SnapshotEncoder(DjangoJSONEncoder):
    def default(self, value):
        if isinstance(value, datetime):
            return value.isoformat()
        return super().default(value)


class Command(BaseCommand):
    help = "Build a new SQLite deployment database with Alex's completed demo history."

    def add_arguments(self, parser):
        parser.add_argument("--since", required=True, type=date.fromisoformat)
        parser.add_argument("--output", required=True, type=Path)

    def handle(self, *args, **options):
        output = options["output"].resolve()
        if output.exists():
            raise CommandError(f"Refusing to overwrite an existing database: {output}")
        since = timezone.make_aware(datetime.combine(options["since"], time.min))
        output.parent.mkdir(parents=True, exist_ok=True)
        with transaction.atomic():
            sessions = TrainingSession.objects.filter(
                user__email=User.DEMO_TRAINEE_EMAIL,
                status=TrainingSession.Statuses.COMPLETED,
                start_time__gte=since,
            ).order_by("pk")
            session_ids = list(sessions.values_list("pk", flat=True))
            objects = list(User.objects.filter(email__in=[
                User.DEMO_TRAINEE_EMAIL, "admin@university.ac.uk",
            ]).order_by("pk"))
            # Copy only demo identities; permissions and credentials are seeded afresh.
            for user in objects:
                user.password = "!"
                user.last_login = None
                user.is_superuser = False
            scenario_model = apps.get_model("training", "Scenario")
            objects.extend(scenario_model.objects.filter(
                Q(is_active=True) | Q(sessions__in=session_ids)
            ).distinct().order_by("pk"))
            objects.extend(sessions)
            # Include every session-owned model, including evaluation and interaction logs.
            for model in apps.get_app_config("training").get_models():
                if model == TrainingSession:
                    continue
                fields = {field.name for field in model._meta.fields}
                if "session" in fields:
                    objects.extend(model.objects.filter(session_id__in=session_ids).order_by("pk"))
            for name, lookup in [
                ("FeedbackScore", "feedback__session_id__in"),
                ("FeedbackReviewItem", "feedback__session_id__in"),
                ("TurnAssessment", "trainee_message__session_id__in"),
                ("BoundaryTurnEvaluation", "run__session_id__in"),
            ]:
                model = apps.get_model("training", name)
                objects.extend(model.objects.filter(**{lookup: session_ids}).order_by("pk"))

            included = {(obj._meta.label_lower, obj.pk) for obj in objects}
            for obj in objects:
                for field in obj._meta.fields:
                    if not field.many_to_one and not field.one_to_one:
                        continue
                    value = getattr(obj, field.attname)
                    target = (field.related_model._meta.label_lower, value)
                    if value is not None and target not in included:
                        if field.null:
                            setattr(obj, field.attname, None)
                        else:
                            raise CommandError(f"Missing required relation: {obj._meta.label}.{field.name}")
            records = serializers.serialize("python", objects)
            for record in records:
                if record["model"] == "accounts.user":
                    record["fields"]["groups"] = []
                    record["fields"]["user_permissions"] = []
            fixture = json.dumps(records, cls=SnapshotEncoder)

        counts = {}
        for obj in objects:
            label = obj._meta.label_lower
            counts[label] = counts.get(label, 0) + 1
        with tempfile.TemporaryDirectory(prefix="peertrain-build-", dir=output.parent) as work:
            work = Path(work)
            fixture_path = work / "history.json"
            fixture_path.write_text(fixture, encoding="utf-8")
            database_path = work / "db.sqlite3"
            env = {**os.environ, "DJANGO_DB_PATH": str(database_path)}
            for command in [
                ["migrate", "--noinput"],
                ["loaddata", str(fixture_path)],
                ["seed_peertrain"],
                ["check"],
            ]:
                try:
                    subprocess.run(
                        [sys.executable, str(settings.BASE_DIR / "manage.py"), *command],
                        env=env, cwd=settings.BASE_DIR, check=True, stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE, text=True, encoding="utf-8",
                    )
                except subprocess.CalledProcessError as error:
                    raise CommandError(f"{command[0]} failed:\n{error.stderr}") from error
            # Exclusive creation also protects against another build using this path.
            with output.open("xb") as destination:
                destination.write(database_path.read_bytes())

        manifest = {
            "since": since.isoformat(),
            "date_field": "start_time",
            "built_at": timezone.now().isoformat(),
            "session_ids": session_ids,
            "copied_records": counts,
            "accounts": ["admin@university.ac.uk", User.DEMO_TRAINEE_EMAIL,
                         "teo.wang@university.ac.uk"],
            "sha256": hashlib.sha256(output.read_bytes()).hexdigest(),
        }
        output.with_suffix(".manifest.json").write_text(
            json.dumps(manifest, indent=2) + "\n", encoding="utf-8",
        )
        self.stdout.write(self.style.SUCCESS(
            f"Built {output} with {len(session_ids)} completed sessions and three accounts. "
            "Source database unchanged."
        ))
