from django.urls import path
from . import views

urlpatterns = [
    path('login/', views.user_login, name='login'),
    path('logout/', views.user_logout),
    path('register/', views.register, name='register'),
    path('dashboard/', views.dashboard, name='dashboard'),  
    path('professor-dashboard/', views.professor_dashboard, name='professor_dashboard'),  
    path('student-dashboard/', views.student_dashboard, name='student_dashboard')
]