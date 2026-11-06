from django.urls import path
from rest_framework.routers import DefaultRouter

from . import views

router = DefaultRouter()
router.register("goods-movements", views.GoodsMovementViewSet, basename="goods-movement")
router.register("account-settings", views.InventoryAccountSettingViewSet, basename="inventory-account-setting")

urlpatterns = [
    path("stock/", views.StockOverviewView.as_view(), name="stock-overview"),
    path("reports/stock-reconciliation/", views.StockReconciliationView.as_view(), name="stock-reconciliation"),
    *router.urls,
]
