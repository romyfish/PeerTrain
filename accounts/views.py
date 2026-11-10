from django.contrib import messages
from django.conf import settings
from django.contrib.auth import login, logout
from django.contrib.auth.decorators import login_required
from django.shortcuts import redirect, render
from django.utils.http import url_has_allowed_host_and_scheme

from .forms import AdminRegistrationForm, LoginForm, TraineeRegistrationForm


def _default_destination(user):
    if user.is_superuser or user.role == user.Roles.ADMIN:
        return "admin_users"
    return settings.LOGIN_REDIRECT_URL


def landing_view(request):
    return render(request, "public/landing.html", {"homepage_navigation": True})


def login_view(request):
    next_url = request.POST.get("next", request.GET.get("next", ""))
    if not url_has_allowed_host_and_scheme(
        next_url, allowed_hosts={request.get_host()}, require_https=request.is_secure()
    ):
        next_url = ""

    if request.user.is_authenticated:
        return redirect(next_url or _default_destination(request.user))

    if request.method == "POST":
        form = LoginForm(request.POST, initial={"request": request})
        if form.is_valid():
            login(request, form.user)
            return redirect(next_url or _default_destination(form.user))
    else:
        form = LoginForm(initial={"request": request})

    return render(request, "auth/login.html", {
        "form": form, "next": next_url,
        "show_demo_accounts": settings.SHOW_DEMO_ACCOUNTS,
    })


def register_view(request):
    if request.user.is_authenticated:
        return redirect(_default_destination(request.user))

    if request.method == "POST":
        form = TraineeRegistrationForm(request.POST)
        if form.is_valid():
            user = form.save()
            messages.success(
                request,
                f"Your trainee account for {user.full_name_or_email} has been created. Please sign in.",
            )
            return redirect("login")
    else:
        form = TraineeRegistrationForm()

    return render(request, "auth/register.html", {"form": form})


def admin_register_view(request):
    if request.user.is_authenticated:
        return redirect(_default_destination(request.user))

    if request.method == "POST":
        form = AdminRegistrationForm(request.POST)
        if form.is_valid():
            user = form.save()
            messages.success(
                request,
                f"Administrator account for {user.full_name_or_email} is ready. Please sign in.",
            )
            return redirect("login")
    else:
        form = AdminRegistrationForm()

    return render(request, "auth/register_admin.html", {"form": form})


@login_required
def logout_view(request):
    logout(request)
    messages.info(request, "You have been signed out.")
    return redirect("landing")
