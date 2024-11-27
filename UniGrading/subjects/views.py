from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth.decorators import login_required
from .models import Subject, Category
from .forms import SubjectForm, CategoryForm

@login_required
def my_subjects(request):
    subjects = Subject.objects.all()
    return render(request, 'my_subjects.html', {'subjects': subjects})

@login_required
def create_subject(request):
    if request.method == 'POST':
        form = SubjectForm(request.POST)
        if form.is_valid():
            subject = form.save(commit=False)
            subject.professor = request.user
            subject.save()
            # Create default categories
            Category.objects.create(subject=subject, name='Lectures')
            Category.objects.create(subject=subject, name='Assignments')
            Category.objects.create(subject=subject, name='Tests')
            return redirect('my_subjects')
    else:
        form = SubjectForm()
    return render(request, 'create_subjects.html', {'form': form})

@login_required
def subject_detail(request, pk):
    subject = get_object_or_404(Subject, pk=pk)
    categories = subject.categories.all()
    if request.method == 'POST':
        form = CategoryForm(request.POST)
        if form.is_valid():
            category = form.save(commit=False)
            category.subject = subject
            category.save()
            return redirect('subject_detail', pk=subject.pk)
    else:
        form = CategoryForm()
    return render(request, 'subject_detail.html', {'subject': subject, 'categories': categories, 'form': form})