# assignments/urls.py
from django.urls import path
from . import views

app_name = "assignments"

urlpatterns = [
    path("<int:subject_id>/", views.AssignmentListView.as_view(), name="assignment_list"),
    path("<int:subject_id>/create/", views.AssignmentCreateView.as_view(), name="create_assignment"),
    path("<int:pk>/detail/", views.AssignmentDetailView.as_view(), name="assignment_detail"),
    path("<int:pk>/edit/", views.AssignmentUpdateView.as_view(), name="edit_assignment"),
    path("<int:pk>/delete/", views.delete_assignment, name="delete_assignment"),
    path("<int:pk>/preview/", views.preview_assignment_file, name="preview_assignment_file"),
    path("<int:pk>/download/", views.download_assignment_file, name="download_assignment_file"),
]
