from django.urls import resolve
from django.utils.deprecation import MiddlewareMixin
from subjects.models import Subject, Category

class BreadcrumbMiddleware(MiddlewareMixin):
    def process_view(self, request, view_func, view_args, view_kwargs):
        # Initialize breadcrumbs and full breadcrumb history if not present
        if 'breadcrumbs' not in request.session:
            request.session['breadcrumbs'] = []
        if 'breadcrumb_history' not in request.session:
            request.session['breadcrumb_history'] = []

        current_url = request.path
        current_view = resolve(current_url).url_name

        # Custom breadcrumb names mapping
        custom_breadcrumb_names = {
            'professor_dashboard': 'Professor Dashboard',
            'my_subjects': 'My Subjects',
            'subject_detail': lambda kwargs: Subject.objects.filter(pk=kwargs.get('pk')).exists() and Subject.objects.get(pk=kwargs.get('pk')).name or None,
            'category_detail': lambda kwargs: Category.objects.filter(pk=kwargs.get('pk')).exists() and Category.objects.get(pk=kwargs.get('pk')).name or None,
        }

        # Pages to exclude from breadcrumbs (e.g., Login)
        excluded_views = {'login', 'logout'}

        # Reset breadcrumbs after login
        if current_view == 'professor_dashboard':
            request.session['breadcrumbs'] = []
            request.session['breadcrumb_history'] = []

        # Skip adding excluded pages to breadcrumbs
        if current_view in excluded_views:
            return None

        # Determine breadcrumb name
        breadcrumb_name = custom_breadcrumb_names.get(
            current_view,
            current_view.replace('_', ' ').title() if current_view else 'Unknown'
        )
        if callable(breadcrumb_name):
            breadcrumb_name = breadcrumb_name(view_kwargs)

        # If the breadcrumb entity no longer exists, skip adding it
        if breadcrumb_name is None:
            return None

        # Get breadcrumb history
        breadcrumb_history = request.session['breadcrumb_history']

        # Filter out invalid (deleted) breadcrumbs from the history
        valid_breadcrumb_history = []
        for breadcrumb in breadcrumb_history:
            if breadcrumb['url'] == current_url or self.is_breadcrumb_valid(breadcrumb):
                valid_breadcrumb_history.append(breadcrumb)

        # Update breadcrumb history with only valid breadcrumbs
        breadcrumb_history = valid_breadcrumb_history
        request.session['breadcrumb_history'] = breadcrumb_history

        # Check if the current URL already exists in history
        breadcrumb_exists_in_history = any(breadcrumb['url'] == current_url for breadcrumb in breadcrumb_history)

        if not breadcrumb_exists_in_history:
            # Add current breadcrumb to the history
            breadcrumb_history.append({'name': breadcrumb_name, 'url': current_url})

        # Update breadcrumb history in the session
        request.session['breadcrumb_history'] = breadcrumb_history

        # Determine breadcrumbs to display
        breadcrumbs = []
        for breadcrumb in breadcrumb_history:
            breadcrumbs.append(breadcrumb)
            if breadcrumb['url'] == current_url:
                break

        # Limit breadcrumbs to the last 5 entries
        breadcrumbs = breadcrumbs[-5:]

        # Update breadcrumbs in the session
        request.session['breadcrumbs'] = breadcrumbs

        return None

    def is_breadcrumb_valid(self, breadcrumb):
        """
        Check if a breadcrumb is still valid (not deleted).
        """
        if 'category_detail' in breadcrumb['url']:
            # Check if the category still exists
            category_id = breadcrumb['url'].split('/')[-1]  
            return Category.objects.filter(pk=category_id).exists()
        elif 'subject_detail' in breadcrumb['url']:
            # Check if the subject still exists
            subject_id = breadcrumb['url'].split('/')[-1]  
            return Subject.objects.filter(pk=subject_id).exists()
        return True
