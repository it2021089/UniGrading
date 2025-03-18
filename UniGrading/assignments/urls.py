from django.urls import path
from .views import AssignmentCreateView, AssignmentListView

app_name = "assignments"

urlpatterns = [
    path('<int:subject_id>/', AssignmentListView.as_view(), name='assignment_list'),
    path('<int:subject_id>/create/', AssignmentCreateView.as_view(), name='create'),  

]
