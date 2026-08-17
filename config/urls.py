"""
URL configuration for the ROSHDA project.

The `urlpatterns` list routes URLs to views. For more information please see:
    https://docs.djangoproject.com/en/5.2/topics/http/urls/
Examples:
Function views
    1. Add an import:  from my_app import views
    2. Add a URL to urlpatterns:  path('', views.home, name='home')
Class-based views
    1. Add an import:  from other_app.views import Home
    2. Add a URL to urlpatterns:  path('', Home.as_view(), name='home')
Including another URLconf
    1. Import the include() function: from django.urls import include, path
    2. Add a URL to urlpatterns:  path('blog/', include('blog.urls'))
"""
from django.contrib import admin
from django.urls import path, include  # <--- 'include' must be here
from django.views.generic import RedirectView

urlpatterns = [
    path('admin/', admin.site.urls),
    # Authentication, roles and profiles. Shares the /api/ prefix with the
    # domain routes below, so the public paths are unchanged by the split.
    path('api/', include('accounts.urls')),
    path('api/', include('radiology.urls')),
    path('api/', include('pharmacy.urls')),
    path('api/', include('records.urls')),
    path('api/', include('comms.urls')),
    path('api/', include('knowledge.urls')),
    path('api/', include('appointments.urls')),  # All endpoints start with /api/
    # Note: MEDIA_URL is deliberately NOT served here. Imaging files are private
    # healthcare data and are reachable only through radiology's download view,
    # which checks the caller's relationship to the study first.
    path('', RedirectView.as_view(url='api/', permanent=False)),
]
