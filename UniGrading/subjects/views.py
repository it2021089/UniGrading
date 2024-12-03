from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from .models import Subject
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
            return redirect('my_subjects')
    else:
        form = SubjectForm()
    return render(request, 'create_subjects.html', {'form': form})

@login_required
def subject_detail(request, pk):
    subject = get_object_or_404(Subject, pk=pk)
    return render(request, 'subject_detail.html', {'subject': subject})