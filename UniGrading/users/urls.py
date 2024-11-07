from django.urls import path
from . import views

urlpatterns = [
    path('login/', views.user_login, name='login'),
    path('logout/', views.user_logout),
    path('register/', views.register, name='register'),
    path('users/', views.users_list, name='users'),
    path('dashboard/', views.dashboard, name='dashboard'),  
    path('admin-dashboard/', views.admin_dashboard, name='admin_dashboard'),  
    path('professor-dashboard/', views.professor_dashboard, name='professor_dashboard'),  
    path('student-dashboard/', views.student_dashboard, name='student_dashboard')
]