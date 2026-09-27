from django.db import migrations, models


def clear_unobserved_overall_scores(apps, schema_editor):
    """A zero score was previously used as a sentinel for missing evidence."""
    Feedback = apps.get_model("training", "Feedback")
    FeedbackScore = apps.get_model("training", "FeedbackScore")
    TrainingSession = apps.get_model("training", "TrainingSession")
    for feedback in Feedback.objects.filter(overall_score=0):
        has_observed_score = FeedbackScore.objects.filter(
            feedback_id=feedback.id,
            observed=True,
            score__isnull=False,
        ).exists()
        if not has_observed_score:
            feedback.overall_score = None
            feedback.save(update_fields=["overall_score"])
            TrainingSession.objects.filter(pk=feedback.session_id, overall_score=0).update(
                overall_score=None
            )


class Migration(migrations.Migration):
    dependencies = [
        ("training", "0019_remove_roleplay_framework_leaks"),
    ]

    operations = [
        migrations.AlterField(
            model_name="feedback",
            name="overall_score",
            field=models.PositiveSmallIntegerField(blank=True, null=True),
        ),
        migrations.RunPython(clear_unobserved_overall_scores, migrations.RunPython.noop),
    ]
