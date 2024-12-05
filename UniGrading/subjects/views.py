from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from .models import Subject, Category
from .forms import SubjectForm

@login_required
def my_subjects(request):
    subjects_list = Subject.objects.all()
    paginator = Paginator(subjects_list, 6)  # Show 6 subjects per page

    page_number = request.GET.get('page')
    subjects = paginator.get_page(page_number)

    return render(request, 'my_subjects.html', {'subjects': subjects})

@login_required
def create_subject(request):
    if request.method == 'POST':
        form = SubjectForm(request.POST)
        if form.is_valid():
            subject = form.save(commit=False)
            subject.professor = request.user
            subject.save()
            categories = request.POST.getlist('categories')
            for category_name in categories:
                Category.objects.create(subject=subject, name=category_name)
            return redirect('my_subjects')
    else:
        form = SubjectForm()
    return render(request, 'create_subjects.html', {'form': form})

@login_required
def subject_detail(request, pk):
    subject = get_object_or_404(Subject, pk=pk)
    if request.method == 'POST':
        if 'name' in request.POST:
            subject.name = request.POST.get('name')
            subject.save()
        elif 'description' in request.POST:
            subject.description = request.POST.get('description')
            subject.save()
        elif 'new_category' in request.POST:
            category_name = request.POST.get('new_category')
            if category_name:
                Category.objects.create(subject=subject, name=category_name)
        elif 'remove_category' in request.POST:
            category_id = request.POST.get('category_id')
            category = get_object_or_404(Category, id=category_id)
            category.delete()
    return render(request, 'subject_detail.html', {'subject': subject})

@login_required
def delete_subject(request, pk):
    subject = get_object_or_404(Subject, pk=pk)
    if request.method == 'POST':
        subject.delete()
        return redirect('my_subjects')
    return redirect('my_subjects')