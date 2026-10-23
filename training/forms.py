from django import forms

from .models import (
    BoundaryTurnEvaluation,
    PlatformSetting,
    Reflection,
)


INPUT_CLASS = "form-control rounded-4 shadow-sm border-0 peertrain-input"
TEXTAREA_CLASS = "form-control rounded-4 shadow-sm border-0 peertrain-input"
SELECT_CLASS = "form-select rounded-4 shadow-sm border-0 peertrain-input"


class StyledModelForm(forms.ModelForm):
    def _set_widget_classes(self):
        for field in self.fields.values():
            if isinstance(field.widget, forms.CheckboxInput):
                field.widget.attrs["class"] = "form-check-input"
            elif isinstance(field.widget, forms.Textarea):
                field.widget.attrs["class"] = TEXTAREA_CLASS
            elif isinstance(field.widget, (forms.Select, forms.SelectMultiple)):
                field.widget.attrs["class"] = SELECT_CLASS
            else:
                field.widget.attrs["class"] = INPUT_CLASS


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


class ReflectionForm(StyledModelForm):
    class Meta:
        model = Reflection
        fields = ["reflection_text", "action_plan"]
        labels = {
            "reflection_text": "What boundary did I notice?",
            "action_plan": "What will I do next time?",
        }
        widgets = {
            "reflection_text": forms.Textarea(
                attrs={
                    "rows": 4,
                    "placeholder": "Describe the boundary moment you noticed in this conversation.",
                }
            ),
            "action_plan": forms.Textarea(
                attrs={
                    "rows": 4,
                    "placeholder": "Write one concrete boundary action you will try next time.",
                }
            ),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._set_widget_classes()


class PlatformSettingForm(StyledModelForm):
    class Meta:
        model = PlatformSetting
        fields = [
            "max_exchanges_per_session",
        ]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._set_widget_classes()


class BoundaryTurnReviewForm(StyledModelForm):
    class Meta:
        model = BoundaryTurnEvaluation
        fields = [
            "review_status",
            "corrected_judgement",
            "corrected_score",
            "reviewer_notes",
        ]
        labels = {
            "review_status": "Review decision",
            "corrected_judgement": "Corrected judgement",
            "corrected_score": "Corrected score",
            "reviewer_notes": "Reviewer notes",
        }
        widgets = {
            "reviewer_notes": forms.Textarea(
                attrs={
                    "rows": 3,
                    "placeholder": "Record the evidence for a correction or rejection.",
                }
            ),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._set_widget_classes()
        self.fields["review_status"].choices = [
            (
                BoundaryTurnEvaluation.ReviewStatuses.CONFIRMED,
                "Confirm the judge result",
            ),
            (
                BoundaryTurnEvaluation.ReviewStatuses.CORRECTED,
                "Correct the judge result",
            ),
            (
                BoundaryTurnEvaluation.ReviewStatuses.REJECTED,
                "Reject this judgement",
            ),
        ]
        self.fields["corrected_judgement"].choices = [
            ("", "Select a corrected judgement"),
            *[
                (value, label)
                for value, label in BoundaryTurnEvaluation.Judgements.choices
                if value != BoundaryTurnEvaluation.Judgements.NOT_APPLICABLE
            ],
        ]
        self.fields["corrected_score"].help_text = (
            "Required only when the review decision is Corrected."
        )

    def clean(self):
        cleaned = super().clean()
        status = cleaned.get("review_status")
        notes = (cleaned.get("reviewer_notes") or "").strip()
        if status in {
            BoundaryTurnEvaluation.ReviewStatuses.CORRECTED,
            BoundaryTurnEvaluation.ReviewStatuses.REJECTED,
        } and not notes:
            self.add_error(
                "reviewer_notes",
                "Explain the evidence for a correction or rejection.",
            )
        if status != BoundaryTurnEvaluation.ReviewStatuses.CORRECTED:
            cleaned["corrected_judgement"] = ""
            cleaned["corrected_score"] = None
        return cleaned
