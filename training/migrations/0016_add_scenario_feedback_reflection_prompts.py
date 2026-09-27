from django.db import migrations


FRAMEWORK_VERSION = "CMCV-1.0"

SCENARIO_LAYERS = {
    "EMOTIONAL": {
        "name": "Agency Boundary",
        "context": (
            "Focus on whether the trainee preserves the help-seeker's choice, explores "
            "preferences, and avoids directive fixing or supporter-centred self-disclosure."
        ),
        "reflection": (
            "Invite reflection on how the trainee preserved choice and avoided taking over."
        ),
    },
    "BURNOUT": {
        "name": "Relationship Boundary",
        "context": (
            "Focus on sustainable availability, privacy, role responsibility, clear limits, "
            "and realistic alternatives to becoming the help-seeker's sole support."
        ),
        "reflection": (
            "Invite reflection on how the trainee balanced warmth with sustainable limits "
            "and offered alternatives to constant availability."
        ),
    },
    "REFERRAL": {
        "name": "Safety Boundary",
        "context": (
            "Focus on risk recognition, role limits, direct safety clarification when "
            "warranted, and warm collaborative escalation without abandonment."
        ),
        "reflection": (
            "Invite reflection on when the trainee noticed risk, how they introduced "
            "escalation, and whether they stayed connected after signposting."
        ),
    },
    "PRIVACY": {
        "name": "Integrated Boundary",
        "context": (
            "Assess Agency, Relationship, and Safety separately while sensitive information "
            "and competing needs require the trainee to balance all three."
        ),
        "reflection": (
            "Invite reflection on how agency, supporter relationship limits, and safety "
            "interacted in the same conversation."
        ),
    },
}


def add_scenario_prompts(apps, schema_editor):
    Prompt = apps.get_model("training", "Prompt")
    Scenario = apps.get_model("training", "Scenario")

    for prompt_type in ("FEEDBACK", "REFLECTION"):
        global_prompt = (
            Prompt.objects.filter(
                type=prompt_type,
                scenario__isnull=True,
                is_active=True,
                is_archived=False,
            )
            .order_by("id")
            .first()
        )
        if global_prompt is None:
            continue

        for scenario in Scenario.objects.filter(
            boundary_type__in=SCENARIO_LAYERS,
            is_active=True,
        ):
            layer = SCENARIO_LAYERS[scenario.boundary_type]
            label = prompt_type.title().replace("_", " ")
            name = f"{layer['name']} {label} Prompt"
            extra = (
                layer["context"]
                if prompt_type == "FEEDBACK"
                else layer["reflection"]
            )
            context = (
                f"{global_prompt.context}\n\n"
                f"Training construct: {layer['name']}.\n"
                f"Practice scenario: {scenario.title}.\n"
                f"Scenario-specific instruction: {extra}"
            ).strip()
            custom_active_exists = (
                Prompt.objects.filter(
                    type=prompt_type,
                    scenario=scenario,
                    is_active=True,
                    is_archived=False,
                )
                .exclude(name=name)
                .exists()
            )
            Prompt.objects.update_or_create(
                name=name,
                defaults={
                    "type": prompt_type,
                    "scenario": scenario,
                    "context": context,
                    "method": global_prompt.method,
                    "framework_version": FRAMEWORK_VERSION,
                    "is_archived": False,
                    "is_active": not custom_active_exists,
                },
            )


class Migration(migrations.Migration):

    dependencies = [
        ("training", "0015_cmcv_prompts_and_reflection_guidance"),
    ]

    operations = [
        migrations.RunPython(add_scenario_prompts, migrations.RunPython.noop),
    ]
