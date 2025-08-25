from django import forms
from .models import Test

class TestForm(forms.ModelForm):
    class Meta:
        model = Test
        fields = ["name", "duration_minutes"]
        widgets = {
            "name": forms.TextInput(attrs={"class": "form-control"}),
            "duration_minutes": forms.NumberInput(attrs={"class": "form-control", "min": 1}),
        }
