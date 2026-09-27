from django.db import migrations


OBSOLETE_PROMPT_NAME = "x'x'x"


def remove_unused_test_prompt(apps, schema_editor):
    Prompt = apps.get_model("training", "Prompt")
    TrainingSession = apps.get_model("training", "TrainingSession")
    Feedback = apps.get_model("training", "Feedback")
    Reflection = apps.get_model("training", "Reflection")
    ReflectionGuidance = apps.get_model("training", "ReflectionGuidance")

    candidates = Prompt.objects.filter(
        name=OBSOLETE_PROMPT_NAME,
        scenario__isnull=True,
        is_active=False,
    )
    for prompt in candidates:
        is_referenced = (
            TrainingSession.objects.filter(prompt=prompt).exists()
            or Feedback.objects.filter(prompt=prompt).exists()
            or Reflection.objects.filter(prompt=prompt).exists()
            or ReflectionGuidance.objects.filter(prompt=prompt).exists()
        )
        if not is_referenced:
            prompt.delete()


class Migration(migrations.Migration):
    dependencies = [
        ("training", "0017_rename_boundary_skills_rubric"),
    ]

    operations = [
        migrations.RunPython(remove_unused_test_prompt, migrations.RunPython.noop),
    ]
