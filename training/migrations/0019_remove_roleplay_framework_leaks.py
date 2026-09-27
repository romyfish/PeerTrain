import re

from django.db import migrations


FRAMEWORK_PREFIX = re.compile(
    r"(?im)^\s*cmcv(?:\s+framework)?(?:\s*[:：-]?\s*\d+(?:\.\d+)*)?\s*[:：-]?\s*(?:\n+|$)"
)


def remove_roleplay_framework_prefixes(apps, schema_editor):
    """Repair existing AI chat messages created before the output-leak guard."""
    Message = apps.get_model("training", "Message")
    for message in Message.objects.filter(sender_type="AI", content__icontains="cmcv"):
        cleaned = FRAMEWORK_PREFIX.sub("", message.content or "", count=1).strip()
        if cleaned and cleaned != message.content:
            message.content = cleaned
            message.save(update_fields=["content"])


class Migration(migrations.Migration):
    dependencies = [
        ("training", "0018_remove_unused_test_prompt"),
    ]

    operations = [
        migrations.RunPython(remove_roleplay_framework_prefixes, migrations.RunPython.noop),
    ]
