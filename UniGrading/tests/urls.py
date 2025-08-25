from django.urls import path
from . import views

app_name = "tests"

urlpatterns = [
    path("<int:subject_id>/", views.my_tests, name="my_tests"),
    path("<int:subject_id>/new/", views.test_detail, name="create_test"),
    path("<int:subject_id>/<int:test_id>/", views.test_detail, name="edit_test"),
]
