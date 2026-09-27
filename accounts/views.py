from django.contrib import messages
from django.contrib.auth import login, logout
from django.shortcuts import redirect, render

from .forms import AdminRegistrationForm, LoginForm, TraineeRegistrationForm


def _redirect_for_user(user):
    if user.is_superuser or user.role == user.Roles.ADMIN:
        return redirect("admin_dashboard")
    return redirect("dashboard")


def landing_view(request):
    if request.user.is_authenticated:
        return _redirect_for_user(request.user)
    return render(request, "public/landing.html")


def login_view(request):
    if request.user.is_authenticated:
        return _redirect_for_user(request.user)

    if request.method == "POST":
        form = LoginForm(request.POST, initial={"request": request})
        if form.is_valid():
            login(request, form.user)
            return _redirect_for_user(form.user)
    else:
        form = LoginForm(initial={"request": request})

    return render(request, "auth/login.html", {"form": form})


def register_view(request):
    if request.user.is_authenticated:
        return _redirect_for_user(request.user)

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
        return _redirect_for_user(request.user)

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


def logout_view(request):
    logout(request)
    messages.info(request, "You have been signed out.")
    return redirect("landing")
