from django.urls import path
from . import views

urlpatterns = [
    path('subjects/', views.my_subjects, name='my_subjects'),
    path('create_subject/', views.create_subject, name='create_subject'),
    path('subject/<int:pk>/', views.subject_detail, name='subject_detail'),
    path('subject/<int:pk>/delete/', views.delete_subject, name='delete_subject'),
]