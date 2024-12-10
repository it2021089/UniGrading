from django.urls import path
from . import views

urlpatterns = [
    path('subjects/', views.my_subjects, name='my_subjects'),
    path('create_subject/', views.create_subject, name='create_subject'),
    path('subject/<int:pk>/', views.subject_detail, name='subject_detail'),
    path('subject/<int:pk>/delete/', views.delete_subject, name='delete_subject'),
    path('category/<int:pk>/', views.category_detail, name='category_detail'),
    path('file/delete/<int:pk>/', views.delete_file, name='delete_file'),
    path('subcategory/delete/<int:pk>/', views.delete_subcategory, name='delete_subcategory'),
]