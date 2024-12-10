from django import template

register = template.Library()

@register.inclusion_tag('breadcrumbs.html', takes_context=True)
def breadcrumbs(context):
    request = context['request']
    return {'breadcrumbs': request.session.get('breadcrumbs', [])}