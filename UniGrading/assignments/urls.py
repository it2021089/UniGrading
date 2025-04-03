from django.urls import path
from .views import AssignmentCreateView, AssignmentListView, AssignmentDetailView, AssignmentUpdateView, delete_assignment

app_name = "assignments"

urlpatterns = [
    path('<int:subject_id>/', AssignmentListView.as_view(), name='assignment_list'),
    path('<int:subject_id>/create/', AssignmentCreateView.as_view(), name='create_assignment'),  
    path('assignment/<int:pk>/', AssignmentDetailView.as_view(), name='assignment_detail'),
    path('assignment/<int:pk>/delete/', delete_assignment, name='delete_assignment'),  
    path('assignment/<int:pk>/edit/', AssignmentUpdateView.as_view(), name='edit_assignment'),  
]
