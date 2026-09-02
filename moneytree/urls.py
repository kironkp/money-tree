"""URL configuration for MoneyTree."""
from django.contrib import admin
from django.urls import include, path

urlpatterns = [
    # Django admin stays registered as the raw-data escape hatch.
    path('admin/', admin.site.urls),
    path('', include('main_app.urls')),
]
