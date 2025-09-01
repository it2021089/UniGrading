from django.urls import path
from . import views

from .views import (
    MySubjectsView, CreateSubjectView, SubjectDetailView,
    CategoryDetailView, delete_file, delete_subcategory, delete_subject,browse_subjects,enroll_subject,
)

app_name = "subjects"

urlpatterns = [
    path("", MySubjectsView.as_view(), name="my_subjects"),
    path("create/", CreateSubjectView.as_view(), name="create_subject"),
    path("subject/<int:pk>/", SubjectDetailView.as_view(), name="subject_detail"),
    path("subject/<int:pk>/delete/", delete_subject, name="delete_subject"),
    path("subject/<int:pk>/rename/", views.rename_subject, name="rename_subject"),  
    path("category/<int:pk>/", CategoryDetailView.as_view(), name="category_detail"),

    path("file/<int:pk>/delete/", delete_file, name="delete_file"),
    path("subcategory/<int:pk>/delete/", delete_subcategory, name="delete_subcategory"),
    path("file/<int:file_id>/download/", views.download_file, name="download_file"),
    path("file/<int:pk>/preview/", views.preview_file, name="preview_file"),
    path("browse/", browse_subjects, name="browse_subjects"),
    path("<int:pk>/enroll/", enroll_subject, name="enroll_subject"),
    path("<int:subject_id>/unenroll/", views.unenroll_subject, name="unenroll_subject"),
    path("subject/<int:pk>/enrollments/", views.SubjectEnrollmentsView.as_view(), name="manage_enrollments"),

]
