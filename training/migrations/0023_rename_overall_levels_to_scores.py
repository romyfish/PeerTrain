from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("training", "0022_canonical_boundary_focus"),
    ]

    operations = [
        migrations.RenameField(
            model_name="trainingsession",
            old_name="overall_level",
            new_name="overall_score",
        ),
        migrations.RenameField(
            model_name="feedback",
            old_name="overall_level",
            new_name="overall_score",
        ),
    ]
