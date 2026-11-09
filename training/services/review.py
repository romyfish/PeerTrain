"""Shared, read-only presentation of current and historical session turns."""


def review_timeline(session, feedback):
    """Number trainee replies in transcript order; following AI replies share that turn."""
    examples = list(feedback.review_items.all()) if feedback else []
    timeline = []
    number = 0
    for message in session.messages.select_related("session__scenario", "assessment").order_by("created_at", "id"):
        is_trainee = message.sender_type == 'USER'
        if is_trainee:
            number += 1
        assessment = getattr(message, 'assessment', None) if is_trainee else None
        turn_examples = [item for item in examples if item.message_id == message.pk] if is_trainee else []
        # Historical reviews may have an assessed alternative without a review item.
        record = assessment.record if assessment else {}
        alternative = record.get('alternative_reply')
        if alternative and not any(item.better_reply == alternative for item in turn_examples):
            turn_examples.append({'better_reply': alternative,
                                  'why_it_matters': record.get('alternative_reason', '')})
        timeline.append({'message': message, 'turn_number': number or None,
                         'turn_label': f'Turn {number}' if number else 'Opening',
                         'show_label': is_trainee or not timeline,
                         'assessment': assessment, 'examples': turn_examples})
    return timeline
