from django.contrib.auth.forms import AuthenticationForm
from django.contrib.auth import get_backends, login, logout
from django.contrib.auth.mixins import LoginRequiredMixin, UserPassesTestMixin
from django.views.generic.edit import FormView
from django.views.generic import DetailView
from django.views.generic import TemplateView
from django.shortcuts import redirect, render
from django.http import HttpResponseRedirect
from .forms import UserRegistrationForm, ProfileForm
from django.urls import reverse, reverse_lazy
from django.contrib.auth.decorators import login_required
from UniGrading.mixin import BreadcrumbMixin

@login_required
def home(request):
    role = request.user.role
    if role == 'professor':
        return redirect('users:professor_dashboard')
    elif role == 'student':
        return redirect('users:student_dashboard')
    else:
        return redirect('users:login')  

# Registration View
class RegisterView(FormView):
    template_name = "register.html"
    form_class = UserRegistrationForm

    def form_valid(self, form):
        user = form.save(commit=False)
        user.set_password(form.cleaned_data["password"])
        user.role = form.cleaned_data["role"]
        user.institution = form.cleaned_data["institution"]
        user.save()

        backend = get_backends()[0]
        user.backend = f"{backend.__module__}.{backend.__class__.__name__}"
        login(self.request, user)

        # Redirect based on the user's role
        if user.role == "professor":
            return HttpResponseRedirect(reverse_lazy("professor_dashboard"))
        elif user.role == "student":
            return HttpResponseRedirect(reverse_lazy("student_dashboard"))
        else:
            return HttpResponseRedirect(reverse_lazy("login"))  

# Login View
class LoginView(FormView):
    template_name = "login.html"
    form_class = AuthenticationForm

    def form_valid(self, form):
        user = form.get_user()
        login(self.request, user)
        role = user.role
        if role == "professor":
            return redirect("users:professor_dashboard")
        elif role == "student":
            return redirect("users:student_dashboard")
        return redirect("users:login")  
    
# Professor Dashboard View
class ProfessorDashboardView(LoginRequiredMixin, UserPassesTestMixin, BreadcrumbMixin, TemplateView):
    template_name = "professor_dashboard.html"
    breadcrumbs = [("Dashboard", "/professor_dashboard/")]

    def test_func(self):
        return self.request.user.role == "professor"


# Student Dashboard View
class StudentDashboardView(LoginRequiredMixin, UserPassesTestMixin, BreadcrumbMixin, TemplateView):
    template_name = "student_dashboard.html"
    breadcrumbs = [("Dashboard", "/student_dashboard/")]

    def test_func(self):
        return self.request.user.role == "student"


class ProfileView(LoginRequiredMixin, BreadcrumbMixin, FormView):
    template_name = "profile.html"
    form_class = ProfileForm

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["instance"] = self.request.user
        return kwargs

    def get_breadcrumbs(self):
        user = self.request.user
        if user.role == "professor":
            return [("Dashboard", "/professor-dashboard/"), ("Profile", "/profile/")]
        elif user.role == "student":
            return [("Dashboard", "/student-dashboard/"), ("Profile", "/profile/")]
        return [("Dashboard", "/dashboard/"), ("Profile", "/profile/")]

    def form_valid(self, form):
        user = form.save(commit=False)
        password = form.cleaned_data.get("password")
        if password:
            user.set_password(password)
        user.save()
       


@login_required
def user_logout(request):
    logout(request)
    return redirect("users:login")

#Login View 
def user_login(request):
    if request.method == 'POST':
        form = AuthenticationForm(data=request.POST)
        if form.is_valid():
            user = form.get_user()
            login(request, user)
            return redirect('dashboard')
    else:
        form = AuthenticationForm()
    return render(request, 'login.html', {'form': form})
