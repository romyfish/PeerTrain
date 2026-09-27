from django.db import migrations


def refresh_system_roleplay_prompts(apps, schema_editor):
    """Refresh only version-controlled Role-play prompts; custom prompts stay untouched."""
    Prompt = apps.get_model("training", "Prompt")
    Scenario = apps.get_model("training", "Scenario")

    from training.content import default_prompt_parts

    global_parts = default_prompt_parts("ROLE_PLAY")
    Prompt.objects.filter(
        name="Default Role Play Prompt",
        type="ROLE_PLAY",
        scenario__isnull=True,
    ).update(
        context=global_parts["context"],
        method=global_parts["method"],
    )

    for scenario in Scenario.objects.all():
        system_name = f"{scenario.title} Role Play Prompt"
        parts = default_prompt_parts("ROLE_PLAY", scenario)
        Prompt.objects.filter(
            name=system_name,
            type="ROLE_PLAY",
            scenario=scenario,
        ).update(
            context=parts["context"],
            method=parts["method"],
        )


class Migration(migrations.Migration):
    dependencies = [
        ("training", "0027_boundaryturnevaluation_review_reason_and_more"),
    ]

    operations = [
        migrations.RunPython(
            refresh_system_roleplay_prompts,
            migrations.RunPython.noop,
        ),
    ]
