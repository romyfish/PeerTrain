from django.db import migrations, models
import django.db.models.deletion


FRAMEWORK_VERSION = "CMCV-1.0"


def _context_for(prompt):
    if prompt.type == "ROLE_PLAY":
        base = (
            "The AI is the help-seeker in a peer-support training conversation. "
            "The trainee is the peer supporter. The help-seeker must remain a believable "
            "first-person character whose disclosure changes in response to the trainee."
        )
    elif prompt.type == "FEEDBACK":
        base = (
            "Assess only the trainee's messages in a peer-support role-play. Use three "
            "boundary constructs: Agency, Relationship, and Safety."
        )
    else:
        base = (
            "Support post-session reflection without writing the trainee's answer. Use the "
            "conversation, feedback evidence, and boundary review moments."
        )
    if prompt.scenario_id:
        return f"{base} Scenario: {prompt.scenario.title}."
    return f"{base} This prompt is a global fallback."


def populate_cmcv_and_archive_redundant_prompts(apps, schema_editor):
    Prompt = apps.get_model("training", "Prompt")
    Feedback = apps.get_model("training", "Feedback")
    Reflection = apps.get_model("training", "Reflection")

    for prompt in Prompt.objects.select_related("scenario"):
        prompt.context = _context_for(prompt)
        prompt.framework_version = FRAMEWORK_VERSION
        prompt.save(update_fields=["context", "framework_version"])

    redundant = Prompt.objects.filter(
        type__in=["FEEDBACK", "REFLECTION"],
        scenario__isnull=False,
    )
    for prompt in redundant:
        type_label = "Feedback" if prompt.type == "FEEDBACK" else "Reflection"
        system_name = f"{prompt.scenario.title} {type_label} Prompt"
        if prompt.name != system_name:
            continue
        referenced = (
            Feedback.objects.filter(prompt_id=prompt.id).exists()
            or Reflection.objects.filter(prompt_id=prompt.id).exists()
        )
        if referenced:
            prompt.is_active = False
            prompt.is_archived = True
            prompt.save(update_fields=["is_active", "is_archived"])
        else:
            prompt.delete()


class Migration(migrations.Migration):

    dependencies = [
        ("training", "0014_alter_feedbackreviewitem_boundary_key_and_more"),
    ]

    operations = [
        migrations.RenameField(
            model_name="prompt",
            old_name="content",
            new_name="method",
        ),
        migrations.AddField(
            model_name="prompt",
            name="context",
            field=models.TextField(blank=True),
        ),
        migrations.AddField(
            model_name="prompt",
            name="framework_version",
            field=models.CharField(default=FRAMEWORK_VERSION, max_length=20),
        ),
        migrations.AddField(
            model_name="prompt",
            name="is_archived",
            field=models.BooleanField(default=False),
        ),
        migrations.RunPython(
            populate_cmcv_and_archive_redundant_prompts,
            migrations.RunPython.noop,
        ),
        migrations.CreateModel(
            name="ReflectionGuidance",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("notice_prompt", models.TextField()),
                ("action_prompt", models.TextField()),
                (
                    "generation_source",
                    models.CharField(
                        choices=[
                            ("OPENAI", "OpenAI"),
                            ("GEMINI", "Google Gemini"),
                            ("PLACEHOLDER", "Placeholder"),
                        ],
                        default="PLACEHOLDER",
                        max_length=20,
                    ),
                ),
                (
                    "framework_version",
                    models.CharField(default=FRAMEWORK_VERSION, max_length=20),
                ),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "prompt",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="reflection_guidance_entries",
                        to="training.prompt",
                    ),
                ),
                (
                    "session",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="reflection_guidance",
                        to="training.trainingsession",
                    ),
                ),
            ],
        ),
    ]
