from django.urls import path
from .views import (
    MySubjectsView, CreateSubjectView, SubjectDetailView,
    CategoryDetailView, delete_file, delete_subcategory, delete_subject
)

app_name = "subjects"

urlpatterns = [
    path("", MySubjectsView.as_view(), name="my_subjects"),
    path("create/", CreateSubjectView.as_view(), name="create_subject"),
    path("subject/<int:pk>/", SubjectDetailView.as_view(), name="subject_detail"),
    path("subject/<int:pk>/delete/", delete_subject, name="delete_subject"), 
    path("category/<int:pk>/", CategoryDetailView.as_view(), name="category_detail"),
    path("file/<int:pk>/delete/", delete_file, name="delete_file"),
    path("subcategory/<int:pk>/delete/", delete_subcategory, name="delete_subcategory"),
]
