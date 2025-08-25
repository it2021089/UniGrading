from django.urls import path
from .views import (
    RegisterView, LoginView, user_logout, 
    DashboardView, ProfileView
)

app_name = "users"

urlpatterns = [
    path('login/', LoginView.as_view(), name='login'),
    path('logout/', user_logout, name='logout'),
    path('register/', RegisterView.as_view(), name='register'),
    path('dashboard/', DashboardView.as_view(), name='dashboard'),  
    path('profile/', ProfileView.as_view(), name='profile'),
]