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

# Dashboard view
@login_required
def dashboard(request):
    role = request.user.profile.role
    if role == 'professor':
        return redirect('professor_dashboard')
    elif role == 'student':
        return redirect('student_dashboard')
    else:
        return redirect('login')

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

