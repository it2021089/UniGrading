from django import forms
from .models import Subject, Category

class SubjectForm(forms.ModelForm):
    class Meta:
        model = Subject
        fields = ['name', 'description']

    categories = forms.CharField(widget=forms.HiddenInput(), required=False)

    def save(self, commit=True):
        subject = super().save(commit=False)
        if commit:
            subject.save()
            categories = self.cleaned_data['categories'].split(',')
            for category_name in categories:
                Category.objects.create(subject=subject, name=category_name)
        return subject