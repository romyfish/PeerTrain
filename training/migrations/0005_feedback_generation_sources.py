from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("training", "0004_feedbackreviewitem"),
    ]

    operations = [
        migrations.AddField(
            model_name="feedback",
            name="generation_source",
            field=models.CharField(
                choices=[
                    ("OPENAI", "OpenAI"),
                    ("GEMINI", "Gemini"),
                    ("PLACEHOLDER", "Placeholder"),
                ],
                default="PLACEHOLDER",
                max_length=20,
            ),
        ),
        migrations.AddField(
            model_name="feedbackreviewitem",
            name="source",
            field=models.CharField(
                choices=[
                    ("OPENAI", "OpenAI"),
                    ("GEMINI", "Gemini"),
                    ("PLACEHOLDER", "Placeholder"),
                ],
                default="PLACEHOLDER",
                max_length=20,
            ),
        ),
    ]
