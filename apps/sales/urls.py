from django.urls import path

from . import views

urlpatterns = [
    path("customers/", views.CustomerList.as_view(), name="customer-list"),
    path("customers/<int:pk>/", views.CustomerDetail.as_view(), name="customer-detail"),
    path("orders/", views.SalesOrderList.as_view(), name="sales-order-list"),
    path("orders/<int:pk>/", views.SalesOrderDetail.as_view(), name="sales-order-detail"),
]
