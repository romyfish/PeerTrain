from django import forms
from django.conf import settings
from django.contrib.auth import authenticate
from django.contrib.auth.forms import UserCreationForm

from .models import User


INPUT_CLASS = (
    "form-control form-control-lg rounded-4 border-0 shadow-sm py-3 px-4 peertrain-input"
)


class StyledFormMixin:
    def _apply_classes(self):
        for field in self.fields.values():
            widget = field.widget
            current = widget.attrs.get("class", "")
            widget.attrs["class"] = f"{current} {INPUT_CLASS}".strip()


class LoginForm(StyledFormMixin, forms.Form):
    email = forms.EmailField(
        label="University Email",
        widget=forms.EmailInput(
            attrs={"placeholder": "you@university.ac.uk", "autocomplete": "email"}
        ),
    )
    password = forms.CharField(
        label="Password",
        widget=forms.PasswordInput(
            attrs={"placeholder": "Enter your password", "autocomplete": "current-password"}
        ),
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.user = None
        self._apply_classes()

    def clean(self):
        cleaned_data = super().clean()
        email = cleaned_data.get("email")
        password = cleaned_data.get("password")
        if email and password:
            self.user = authenticate(self.request, username=email, password=password)
            if self.user is None:
                raise forms.ValidationError("Incorrect email or password.")
        return cleaned_data

    @property
    def request(self):
        return self.initial.get("request")


class BaseRegistrationForm(StyledFormMixin, UserCreationForm):
    full_name = forms.CharField(
        label="Full Name",
        widget=forms.TextInput(attrs={"placeholder": "Alex Chen"}),
    )
    email = forms.EmailField(
        label="University Email",
        widget=forms.EmailInput(attrs={"placeholder": "you@university.ac.uk"}),
    )

    class Meta:
        model = User
        fields = ("full_name", "email", "password1", "password2")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["password1"].widget.attrs["placeholder"] = "Create a strong password"
        self.fields["password2"].widget.attrs["placeholder"] = "Repeat your password"
        self._apply_classes()

    def clean_email(self):
        email = self.cleaned_data["email"].strip().lower()
        if User.objects.filter(email=email).exists():
            raise forms.ValidationError("An account with this email already exists.")
        return email

    def save(self, commit=True):
        user = super().save(commit=False)
        email = self.cleaned_data["email"].strip().lower()
        full_name = self.cleaned_data["full_name"].strip()
        user.email = email
        user.username = email
        name_parts = full_name.split(maxsplit=1)
        user.first_name = name_parts[0]
        user.last_name = name_parts[1] if len(name_parts) > 1 else ""
        if commit:
            user.save()
        return user


class TraineeRegistrationForm(BaseRegistrationForm):
    def save(self, commit=True):
        user = super().save(commit=False)
        user.role = User.Roles.TRAINEE
        user.is_staff = False
        if commit:
            user.save()
        return user


class AdminRegistrationForm(BaseRegistrationForm):
    admin_passcode = forms.CharField(
        label="Admin Registration Code",
        widget=forms.PasswordInput(
            attrs={"placeholder": "Enter the administrator registration code"}
        ),
    )

    class Meta(BaseRegistrationForm.Meta):
        fields = ("full_name", "email", "admin_passcode", "password1", "password2")

    def clean_admin_passcode(self):
        code = self.cleaned_data["admin_passcode"]
        if code != settings.ADMIN_REGISTRATION_CODE:
            raise forms.ValidationError("The admin registration code is incorrect.")
        return code

    def save(self, commit=True):
        user = super().save(commit=False)
        user.role = User.Roles.ADMIN
        user.is_staff = True
        if commit:
            user.save()
        return user
