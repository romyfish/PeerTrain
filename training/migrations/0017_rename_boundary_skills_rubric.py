from django.db import migrations, models


OLD_VERSION = "PT-BR-" + "2.0"
NEW_VERSION = "BOUNDARY-SKILLS-1.0"


def rename_rubric_version(apps, schema_editor):
    for model_name in ("TrainingSession", "Feedback", "FeedbackScore"):
        model = apps.get_model("training", model_name)
        model.objects.filter(rubric_version=OLD_VERSION).update(
            rubric_version=NEW_VERSION
        )


def restore_rubric_version(apps, schema_editor):
    for model_name in ("TrainingSession", "Feedback", "FeedbackScore"):
        model = apps.get_model("training", model_name)
        model.objects.filter(rubric_version=NEW_VERSION).update(
            rubric_version=OLD_VERSION
        )


class Migration(migrations.Migration):
    dependencies = [
        ("training", "0016_add_scenario_feedback_reflection_prompts"),
    ]

    operations = [
        migrations.AlterField(
            model_name="trainingsession",
            name="rubric_version",
            field=models.CharField(
                default=NEW_VERSION,
                max_length=30,
            ),
        ),
        migrations.AlterField(
            model_name="feedback",
            name="rubric_version",
            field=models.CharField(
                default=NEW_VERSION,
                max_length=30,
            ),
        ),
        migrations.AlterField(
            model_name="feedbackscore",
            name="rubric_version",
            field=models.CharField(
                default=NEW_VERSION,
                max_length=30,
            ),
        ),
        migrations.RunPython(
            rename_rubric_version,
            restore_rubric_version,
        ),
    ]
