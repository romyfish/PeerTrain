import json
import sqlite3
import tempfile
from contextlib import closing
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.urls import reverse

from training.models import Feedback, Message, Reflection, Scenario, TrainingSession


class DeploymentTests(TestCase):
    @override_settings(SHOW_DEMO_ACCOUNTS=False)
    def test_cloud_login_does_not_disclose_demo_credentials(self):
        response = self.client.get(reverse("login"))
        self.assertEqual(response.status_code, 200)
        for text in ["Demo accounts", "password123", "alex.chen@", "admin@"]:
            self.assertNotContains(response, text)

    def test_deployment_database_filters_history_and_keeps_related_records(self):
        call_command("seed_peertrain", verbosity=0)
        user_model = get_user_model()
        alex = user_model.objects.get(email="alex.chen@university.ac.uk")
        scenario = Scenario.objects.first()
        included = TrainingSession.objects.create(
            user=alex, scenario=scenario, status=TrainingSession.Statuses.COMPLETED,
        )
        cutoff = datetime(2026, 11, 1, tzinfo=ZoneInfo("Europe/London"))
        TrainingSession.objects.filter(pk=included.pk).update(start_time=cutoff)
        old = TrainingSession.objects.create(
            user=alex, scenario=scenario, status=TrainingSession.Statuses.COMPLETED,
        )
        TrainingSession.objects.filter(pk=old.pk).update(
            start_time=cutoff.replace(month=10, day=31),
        )
        TrainingSession.objects.create(user=alex, scenario=scenario)
        other = user_model.objects.create_user(
            username="other@example.com", email="other@example.com",
        )
        TrainingSession.objects.create(
            user=other, scenario=scenario, status=TrainingSession.Statuses.COMPLETED,
        )
        message = Message.objects.create(session=included, sender_type="AI", content="Demo message")
        Feedback.objects.create(session=included, strengths="Strength", improvements="Improve")
        Reflection.objects.create(session=included, reflection_text="Demo reflection")
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "demo.sqlite3"
            call_command("build_deployment_db", since=cutoff.date(), output=output)
            with closing(sqlite3.connect(output)) as database:
                self.assertEqual(database.execute("PRAGMA integrity_check").fetchone()[0], "ok")
                self.assertEqual(database.execute("PRAGMA foreign_key_check").fetchall(), [])
                self.assertEqual(database.execute("SELECT id FROM training_trainingsession").fetchall(), [(included.pk,)])
                self.assertEqual(database.execute("SELECT content, created_at FROM training_message").fetchone(),
                                 ("Demo message", message.created_at.replace(tzinfo=None).isoformat(" ")))
                self.assertEqual(database.execute("SELECT COUNT(*) FROM training_feedback").fetchone()[0], 1)
                self.assertEqual(database.execute("SELECT reflection_text FROM training_reflection").fetchone()[0], "Demo reflection")
                accounts = database.execute("SELECT email, password, role FROM accounts_user").fetchall()
                self.assertEqual({row[0] for row in accounts}, {
                    "alex.chen@university.ac.uk", "admin@university.ac.uk", "teo.wang@university.ac.uk",
                })
                from django.contrib.auth.hashers import check_password
                self.assertTrue(all(check_password("password123", row[1]) for row in accounts))
                self.assertEqual(next(row[2] for row in accounts if row[0].startswith("teo.")), "TRAINEE")
            manifest = json.loads(output.with_suffix(".manifest.json").read_text())
            self.assertEqual(manifest["session_ids"], [included.pk])
        # Building the artifact must not seed, delete, or reset source records.
        self.assertEqual(TrainingSession.objects.count(), 4)
        self.assertTrue(user_model.objects.filter(pk=other.pk).exists())
