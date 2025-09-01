# assignments/forms.py
from django import forms
from .models import Assignment

class AssignmentForm(forms.ModelForm):
    class Meta:
        model = Assignment
        fields = ["title","description","due_date","file","autograde_enabled"]
        widgets = {
            "due_date": forms.DateTimeInput(attrs={"type":"datetime-local"}),
        }
