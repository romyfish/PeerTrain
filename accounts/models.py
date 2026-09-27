from django.contrib.auth.models import AbstractUser
from django.db import models


class User(AbstractUser):
    DEMO_TRAINEE_EMAIL = "alex.chen@university.ac.uk"

    class Roles(models.TextChoices):
        ADMIN = "ADMIN", "Admin"
        TRAINEE = "TRAINEE", "Trainee"

    email = models.EmailField(unique=True)
    role = models.CharField(
        max_length=20,
        choices=Roles.choices,
        default=Roles.TRAINEE,
    )

    def save(self, *args, **kwargs):
        if self.email:
            self.email = self.email.lower().strip()
            self.username = self.email
        if self.is_superuser:
            self.role = self.Roles.ADMIN
        self.is_staff = self.role == self.Roles.ADMIN or self.is_superuser
        super().save(*args, **kwargs)

    @property
    def full_name_or_email(self):
        return self.get_full_name() or self.email

    @property
    def initials(self):
        name = self.get_full_name().strip()
        if not name:
            return self.email[:2].upper()
        parts = name.split()
        if len(parts) == 1:
            return parts[0][:2].upper()
        return f"{parts[0][0]}{parts[-1][0]}".upper()

    @property
    def is_test_account(self):
        email = (self.email or "").lower().strip()
        return self.role == self.Roles.TRAINEE and email == self.DEMO_TRAINEE_EMAIL

    def __str__(self):
        return self.full_name_or_email
