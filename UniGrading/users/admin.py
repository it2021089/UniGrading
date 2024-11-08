from django.contrib import admin
from django.contrib.auth.admin import UserAdmin
from .models import CustomUser, Institution

class CustomUserAdmin(UserAdmin):
    fieldsets = UserAdmin.fieldsets + (
        (None, {'fields': ('role', 'institution')}),
    )

admin.site.register(CustomUser, CustomUserAdmin)
admin.site.register(Institution)