from django import forms
from django.contrib.auth.models import User
from .models import Institution
from django.contrib.auth.forms import AuthenticationForm

class UserRegistrationForm(forms.ModelForm):
    password = forms.CharField(widget=forms.PasswordInput)
    role = forms.ChoiceField(choices=[('Student', 'Student'), ('Professor', 'Professor')])
    institution = forms.ModelChoiceField(queryset=Institution.objects.all(), required=True)

    class Meta:
        model = User
        fields = ['username', 'email', 'password']
        
    def clean(self):
        cleaned_data = super().clean()
        password = cleaned_data.get('password')
        confirm_password = cleaned_data.get('confirm_password')
        
        if password != confirm_password:
            self.add_error('confirm_password', "Passwords do not match")

        return cleaned_data
    
class CustomAuthenticationForm(AuthenticationForm):
    username = forms.CharField(label='Username or Email', widget=forms.TextInput(attrs={'placeholder': 'Enter your username or email'}))