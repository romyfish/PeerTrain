from django.db import migrations, models


LEGACY_TO_CANONICAL = {
    "EMOTIONAL": "AGENCY",
    "BURNOUT": "RELATIONSHIP",
    "REFERRAL": "SAFETY",
    "PRIVACY": "INTEGRATED",
}


def canonicalise_boundary_focus(apps, schema_editor):
    Scenario = apps.get_model("training", "Scenario")
    for legacy, canonical in LEGACY_TO_CANONICAL.items():
        Scenario.objects.filter(boundary_type=legacy).update(boundary_type=canonical)


def restore_legacy_boundary_focus(apps, schema_editor):
    Scenario = apps.get_model("training", "Scenario")
    for legacy, canonical in LEGACY_TO_CANONICAL.items():
        Scenario.objects.filter(boundary_type=canonical).update(boundary_type=legacy)


class Migration(migrations.Migration):
    dependencies = [
        ("training", "0021_boundary_skills_rubric_2"),
    ]

    operations = [
        migrations.RunPython(
            canonicalise_boundary_focus,
            restore_legacy_boundary_focus,
        ),
        migrations.AlterField(
            model_name="scenario",
            name="boundary_type",
            field=models.CharField(
                choices=[
                    ("AGENCY", "Agency Boundary"),
                    ("RELATIONSHIP", "Relationship Boundary"),
                    ("SAFETY", "Safety Boundary"),
                    ("INTEGRATED", "Integrated Boundary"),
                ],
                max_length=30,
            ),
        ),
    ]
