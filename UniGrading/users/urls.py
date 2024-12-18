from django.urls import path
from .views import (
    RegisterView, LoginView, user_logout, 
    ProfessorDashboardView, StudentDashboardView, ProfileView
)

app_name = "users"

urlpatterns = [
    path('login/', LoginView.as_view(), name='login'),
    path('logout/', user_logout, name='logout'),
    path('register/', RegisterView.as_view(), name='register'),
    path('professor-dashboard/', ProfessorDashboardView.as_view(), name='professor_dashboard'),  
    path('student-dashboard/', StudentDashboardView.as_view(), name='student_dashboard'),
    path('profile/', ProfileView.as_view(), name='profile'),
]