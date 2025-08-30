# assignments/forms.py
from django import forms
from .models import Assignment

class AssignmentForm(forms.ModelForm):
    due_date = forms.DateTimeField(
        input_formats=["%d/%m/%Y %H:%M", "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M"],
        widget=forms.TextInput(attrs={
            "placeholder": "DD/MM/YYYY HH:MM",
            "autocomplete": "off",
        })
    )

    class Meta:
        model = Assignment
        fields = ["title", "description", "due_date", "file"]
