from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [("training", "0033_feedback_overview")]

    operations = [
        migrations.RemoveField(model_name="trainingsession", name="feedback_mode"),
    ]
