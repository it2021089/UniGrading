class BreadcrumbMixin:
    def get_breadcrumbs(self):
        return getattr(self, "breadcrumbs", [])

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["breadcrumbs"] = self.get_breadcrumbs()
        return context