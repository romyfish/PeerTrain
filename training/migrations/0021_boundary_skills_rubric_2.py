from django.db import migrations, models
import django.core.validators


def clear_legacy_overall_values(apps, schema_editor):
    TrainingSession = apps.get_model("training", "TrainingSession")
    Feedback = apps.get_model("training", "Feedback")
    FeedbackScore = apps.get_model("training", "FeedbackScore")

    # Legacy values were percentages or LLM-authored grades and are not comparable
    # with the deterministic 1-5 rubric.
    TrainingSession.objects.update(overall_level=None)
    Feedback.objects.update(overall_level=None)
    FeedbackScore.objects.update(
        score=None,
        observed=False,
        rubric_version="BOUNDARY-SKILLS-2.0",
    )


class Migration(migrations.Migration):
    dependencies = [
        ("training", "0020_feedback_overall_score_nullable"),
    ]

    operations = [
        migrations.RenameField(
            model_name="trainingsession",
            old_name="overall_score",
            new_name="overall_level",
        ),
        migrations.AlterField(
            model_name="trainingsession",
            name="overall_level",
            field=models.PositiveSmallIntegerField(
                blank=True,
                null=True,
                validators=[
                    django.core.validators.MinValueValidator(1),
                    django.core.validators.MaxValueValidator(5),
                ],
            ),
        ),
        migrations.RenameField(
            model_name="feedback",
            old_name="overall_score",
            new_name="overall_level",
        ),
        migrations.AlterField(
            model_name="feedback",
            name="overall_level",
            field=models.PositiveSmallIntegerField(
                blank=True,
                null=True,
                validators=[
                    django.core.validators.MinValueValidator(1),
                    django.core.validators.MaxValueValidator(5),
                ],
            ),
        ),
        migrations.AlterField(
            model_name="trainingsession",
            name="rubric_version",
            field=models.CharField(
                default="BOUNDARY-SKILLS-2.0",
                max_length=30,
            ),
        ),
        migrations.AlterField(
            model_name="feedback",
            name="rubric_version",
            field=models.CharField(
                default="BOUNDARY-SKILLS-2.0",
                max_length=30,
            ),
        ),
        migrations.AlterField(
            model_name="feedbackscore",
            name="rubric_version",
            field=models.CharField(
                default="BOUNDARY-SKILLS-2.0",
                max_length=30,
            ),
        ),
        migrations.AlterField(
            model_name="feedbackscore",
            name="score",
            field=models.PositiveSmallIntegerField(
                blank=True,
                null=True,
                validators=[
                    django.core.validators.MinValueValidator(1),
                    django.core.validators.MaxValueValidator(5),
                ],
            ),
        ),
        migrations.RunPython(clear_legacy_overall_values, migrations.RunPython.noop),
    ]
