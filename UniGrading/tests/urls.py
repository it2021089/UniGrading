# tests/urls.py
from django.urls import path
from . import views

app_name = "tests"

urlpatterns = [
    path("<int:subject_id>/", views.my_tests, name="my_tests"),

    # professor
    path("<int:subject_id>/new/", views.test_detail, name="create_test"),
    path("<int:subject_id>/<int:test_id>/edit/", views.test_detail, name="edit_test"),

    # student
    path("<int:subject_id>/<int:test_id>/take/", views.take_test, name="take_test"),
    path("<int:subject_id>/<int:test_id>/submit/", views.submit_attempt, name="submit_attempt"),

    # professor insights
    path("<int:subject_id>/<int:test_id>/submissions/", views.test_submissions, name="test_submissions"),
    path("<int:subject_id>/<int:test_id>/submissions/<int:attempt_id>/", views.test_attempt_detail, name="test_attempt_detail"),
    path("<int:subject_id>/<int:test_id>/analytics/", views.test_analytics, name="test_analytics"),
]
