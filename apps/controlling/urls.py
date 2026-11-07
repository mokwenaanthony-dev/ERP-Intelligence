from django.urls import path
from rest_framework.routers import DefaultRouter

from . import views

router = DefaultRouter()
router.register("profit-centers", views.ProfitCenterViewSet, basename="profit-center")
router.register("cost-centers", views.CostCenterViewSet, basename="cost-center")
router.register("plant-profit-centers", views.PlantProfitCenterViewSet, basename="plant-profit-center")

urlpatterns = [
    path("reports/cost-centers/", views.CostCenterReportView.as_view(), name="cost-center-report"),
    path("reports/profit-centers/", views.ProfitCenterReportView.as_view(), name="profit-center-report"),
    *router.urls,
]
