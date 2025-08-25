# users/views.py
from django.contrib import messages
from django.contrib.auth.forms import AuthenticationForm
from django.contrib.auth import get_backends, login, logout
from django.contrib.auth.decorators import login_required
from django.contrib.auth.mixins import LoginRequiredMixin
from django.views.generic.edit import FormView
from django.views.generic import TemplateView
from django.shortcuts import redirect, render, get_object_or_404
from django.urls import reverse_lazy
from UniGrading.mixin import BreadcrumbMixin

from .forms import UserRegistrationForm, ProfileForm


@login_required
def home(request):
    # Any authenticated user goes to the unified dashboard
    return redirect("users:dashboard")


# ---------- Registration ----------
class RegisterView(FormView):
    template_name = "register.html"
    form_class = UserRegistrationForm

    def form_valid(self, form):
        user = form.save(commit=False)
        user.set_password(form.cleaned_data["password"])
        user.role = form.cleaned_data["role"]
        user.institution = form.cleaned_data["institution"]
        user.save()

        # Log the user in immediately after signup
        backend = get_backends()[0]
        user.backend = f"{backend.__module__}.{backend.__class__.__name__}"
        login(self.request, user)

        return redirect("users:dashboard")


# ---------- Login (CBV) ----------
class LoginView(FormView):
    template_name = "login.html"
    form_class = AuthenticationForm

    def form_valid(self, form):
        user = form.get_user()
        login(self.request, user)
        return redirect("users:dashboard")


# ---------- Dashboard (single for all) ----------
class DashboardView(LoginRequiredMixin, BreadcrumbMixin, TemplateView):
    template_name = "dashboard.html"

    def get_breadcrumbs(self):
        return [("Dashboard", reverse_lazy("users:dashboard"))]


# ---------- Profile ----------
class ProfileView(LoginRequiredMixin, BreadcrumbMixin, FormView):
    template_name = "profile.html"
    form_class = ProfileForm

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["instance"] = self.request.user
        return kwargs

    def get_breadcrumbs(self):
        return [
            ("Dashboard", reverse_lazy("users:dashboard")),
            ("Profile", ""),
        ]

    def form_valid(self, form):
        user = form.save(commit=False)
        password = form.cleaned_data.get("password")
        if password:
            user.set_password(password)
        user.save()
        messages.success(self.request, "Profile updated successfully.")
        return redirect("users:profile")  


# ---------- Logout ----------
@login_required
def user_logout(request):
    logout(request)
    return redirect("users:login")


# ---------- Login (FBV alternative) ----------
def user_login(request):
    if request.method == "POST":
        form = AuthenticationForm(data=request.POST)
        if form.is_valid():
            user = form.get_user()
            login(request, user)
            return redirect("users:dashboard")
    else:
        form = AuthenticationForm()
    return render(request, "login.html", {"form": form})
