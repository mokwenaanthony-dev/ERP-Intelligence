from rest_framework.routers import DefaultRouter

from . import views

router = DefaultRouter()
router.register("company-codes", views.CompanyCodeViewSet, basename="company-code")
router.register("plants", views.PlantViewSet, basename="plant")
router.register("storage-locations", views.StorageLocationViewSet, basename="storage-location")
router.register("sales-organizations", views.SalesOrganizationViewSet, basename="sales-organization")
router.register("purchasing-organizations", views.PurchasingOrganizationViewSet, basename="purchasing-organization")

urlpatterns = router.urls
