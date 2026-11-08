from django import forms


class ChatMessageForm(forms.Form):
    content = forms.CharField(
        label="Your response",
        widget=forms.Textarea(
            attrs={
                "rows": 1,
                "placeholder": "Message the AI help-seeker...",
                "class": "peertrain-chat-input",
                "autocomplete": "off",
            }
        ),
    )
