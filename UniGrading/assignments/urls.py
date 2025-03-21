from django.urls import path
from .views import AssignmentCreateView, AssignmentListView
from .views import AssignmentDetailView
from .views import AssignmentUpdateView
from . import views

app_name = "assignments"

urlpatterns = [
    path('<int:subject_id>/', AssignmentListView.as_view(), name='assignment_list'),
    path('<int:subject_id>/create/', AssignmentCreateView.as_view(), name='create'),  
    path('assignment/<int:pk>/', AssignmentDetailView.as_view(), name='assignment_detail'),
    path('assignments/<int:assignment_id>/delete/', views.delete_assignment, name='delete_assignment'),
    path('assignments/<int:assignment_id>/edit/', AssignmentUpdateView.as_view(), name='assignment_edit'),

]
