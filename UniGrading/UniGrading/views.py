from django.shortcuts import redirect
from django.contrib.auth.decorators import login_required

@login_required
def home(request):
    role = request.user.profile.role
    if role == 'admin':
        return redirect('admin_dashboard')
    elif role == 'professor':
        return redirect('professor_dashboard')
    elif role == 'student':
        return redirect('student_dashboard')
    else:
        return redirect('login')  