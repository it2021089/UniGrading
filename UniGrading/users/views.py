# users/views.py
from django.shortcuts import render, redirect
from django.contrib.auth import login, logout, authenticate
from django.contrib.auth.decorators import login_required, user_passes_test
from django.contrib.auth.forms import AuthenticationForm
from .forms import UserRegistrationForm, CustomAuthenticationForm
from .models import Profile, Institution

# Registration view
def register(request):
    if request.method == 'POST':
        form = UserRegistrationForm(request.POST)
        if form.is_valid():
            user = form.save(commit=False)
            user.set_password(form.cleaned_data['password'])
            user.save()
            
            role = form.cleaned_data['role']
            institution = form.cleaned_data['institution']
            Profile.objects.create(user=user, role=role, institution=institution)
            
            login(request, user)  # Log the user in automatically after registration
            return redirect('dashboard')  # Redirect to dashboard after registration
    else:
        form = UserRegistrationForm()
    
    return render(request, 'register.html', {'form': form})

# Login view
def user_login(request):
    if request.method == 'POST':
        form = CustomAuthenticationForm(data=request.POST)
        if form.is_valid():
            user = form.get_user()
            login(request, user)
            return redirect('dashboard')
    else:
        form = CustomAuthenticationForm()
    return render(request, 'login.html', {'form': form})

# Logout view
def user_logout(request):
    logout(request)
    return redirect('login')

# Users list view
@login_required
@user_passes_test(lambda u: u.profile.role == 'admin')
def users_list(request):
    profiles = Profile.objects.all()
    institutions = Institution.objects.all()
    return render(request, 'users.html', {'profiles': profiles, 'institutions': institutions})

# Dashboard view
@login_required
def dashboard(request):
    role = request.user.profile.role
    if role == 'admin':
        return redirect('admin_dashboard')
    elif role == 'professor':
        return redirect('professor_dashboard')
    elif role == 'student':
        return redirect('student_dashboard')
    else:
        return redirect('login')

# Admin dashboard view
@login_required
@user_passes_test(lambda u: u.profile.role == 'admin')
def admin_dashboard(request):
    return render(request, 'admin_dashboard.html')

# Professor dashboard view
@login_required
@user_passes_test(lambda u: u.profile.role == 'professor')
def professor_dashboard(request):
    return render(request, 'professor_dashboard.html')

# Student dashboard view
@login_required
@user_passes_test(lambda u: u.profile.role == 'student')
def student_dashboard(request):
    return render(request, 'student_dashboard.html')

# Institution creation (admin only)
@login_required
@user_passes_test(lambda u: u.profile.role == 'admin')
def create_institution(request):
    if request.method == 'POST':
        form = InstitutionForm(request.POST)
        if form.is_valid():
            form.save()
            return redirect('institution_list')  # Redirect to institution list after creation
    else:
        form = InstitutionForm()
    return render(request, 'create_institution.html', {'form': form})

# Institution list view (admin only)
@login_required
@user_passes_test(lambda u: u.profile.role == 'admin')
def institution_list(request):
    institutions = Institution.objects.all()
    return render(request, 'institution_list.html', {'institutions': institutions})
