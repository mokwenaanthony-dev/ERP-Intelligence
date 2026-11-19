"""Routes of side-by-side mode (Wave 10b); see ``views`` for what each one does and who may use it."""
from django.urls import path

from . import views

urlpatterns = [
    path("sources/", views.SourceView.as_view(), name="connect-sources"),
    path("imports/", views.ImportView.as_view(), name="connect-imports"),
    path("imports/<int:pk>/", views.ImportDetailView.as_view(), name="connect-import"),
]
