from django.shortcuts import redirect
from django.contrib.auth.decorators import login_required
from users.models import CustomUser

@login_required
def home(request):
    role = request.user.role
    if role == 'professor':
        return redirect('professor_dashboard')
    elif role == 'student':
        return redirect('student_dashboard')
    else:
        return redirect('login')  