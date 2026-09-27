from django.db import migrations, models


BOUNDARY_NAMES = {
    "EMOTIONAL": ("Emotional Boundary", "Relational Focus Boundary"),
    "PRIVACY": ("Privacy Boundary", "Privacy & Confidentiality Boundary"),
    "REFERRAL": ("Referral Boundary", "Role Scope & Escalation Boundary"),
    "BURNOUT": ("Burnout & Compassion Fatigue", "Time & Availability Boundary"),
}

PROMPT_LABELS = ("Role Play", "Feedback", "Reflection")


def rename_system_content(apps, schema_editor):
    Scenario = apps.get_model("training", "Scenario")
    Prompt = apps.get_model("training", "Prompt")

    for boundary_type, (old_name, new_name) in BOUNDARY_NAMES.items():
        scenario = (
            Scenario.objects.filter(boundary_type=boundary_type, title=old_name)
            .order_by("id")
            .first()
        )
        if scenario is None:
            continue

        if not Scenario.objects.exclude(pk=scenario.pk).filter(title=new_name).exists():
            scenario.title = new_name
            scenario.save(update_fields=["title"])

        for prompt_label in PROMPT_LABELS:
            old_prompt_name = f"{old_name} {prompt_label} Prompt"
            new_prompt_name = f"{new_name} {prompt_label} Prompt"
            prompt = Prompt.objects.filter(
                scenario_id=scenario.id,
                name=old_prompt_name,
            ).first()
            if prompt and not Prompt.objects.exclude(pk=prompt.pk).filter(name=new_prompt_name).exists():
                prompt.name = new_prompt_name
                prompt.save(update_fields=["name"])


class Migration(migrations.Migration):
    dependencies = [
        ("training", "0011_remove_scenario_difficulty"),
    ]

    operations = [
        migrations.AlterField(
            model_name="scenario",
            name="boundary_type",
            field=models.CharField(
                choices=[
                    ("EMOTIONAL", "Relational Focus Boundary"),
                    ("PRIVACY", "Privacy & Confidentiality Boundary"),
                    ("REFERRAL", "Role Scope & Escalation Boundary"),
                    ("BURNOUT", "Time & Availability Boundary"),
                ],
                max_length=30,
            ),
        ),
        migrations.RunPython(rename_system_content, migrations.RunPython.noop),
    ]
