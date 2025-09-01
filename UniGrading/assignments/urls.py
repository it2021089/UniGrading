# assignments/urls.py
from django.urls import path
from . import views

app_name = "assignments"

urlpatterns = [
    # Lists & CRUD
    path("<int:subject_id>/", views.AssignmentListView.as_view(), name="assignment_list"),
    path("<int:subject_id>/create/", views.AssignmentCreateView.as_view(), name="create_assignment"),
    path("<int:pk>/detail/", views.AssignmentDetailView.as_view(), name="assignment_detail"),
    path("<int:pk>/edit/", views.AssignmentUpdateView.as_view(), name="edit_assignment"),
    path("<int:pk>/delete/", views.delete_assignment, name="delete_assignment"),

    # Assignment file streaming
    path("<int:pk>/file/preview/", views.preview_assignment_file, name="preview_assignment_file"),
    path("<int:pk>/file/download/", views.download_assignment_file, name="download_assignment_file"),

    # Submissions (student)
    path("<int:pk>/submit/", views.submit_assignment, name="submit_assignment"),
    path("submission/<int:pk>/preview/", views.preview_submission_file, name="preview_submission_file"),
    path("submission/<int:pk>/download/", views.download_submission_file, name="download_submission_file"),

    # Submissions (professor views)
    path("<int:pk>/submissions/", views.AssignmentSubmissionsListView.as_view(), name="assignment_submissions"),
    path("submission/<int:pk>/detail/", views.AssignmentSubmissionDetailView.as_view(), name="assignment_submission_detail"),
    path("submission/<int:pk>/update/", views.update_submission_grade, name="update_submission_grade"),
    path("assignment/<int:pk>/analytics/", views.AssignmentAnalyticsView.as_view(), name="assignment_analytics"),


    
]
