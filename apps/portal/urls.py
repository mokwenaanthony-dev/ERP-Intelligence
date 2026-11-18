"""Routes of the partner portal (Wave 10a); see ``views`` for what each one does and who may use it."""
from django.urls import path

from . import views

urlpatterns = [
    path("me/", views.MeView.as_view(), name="portal-me"),
    path("purchase-orders/", views.PurchaseOrderListView.as_view(), name="portal-purchase-orders"),
    path("purchase-orders/<str:number>/", views.PurchaseOrderView.as_view(), name="portal-purchase-order"),
    path("purchase-orders/<str:number>/confirm/", views.ConfirmView.as_view(), name="portal-confirm"),
    path("delivery-notices/", views.DeliveryNoticeView.as_view(), name="portal-delivery-notices"),
    path("rfqs/", views.RFQListView.as_view(), name="portal-rfqs"),
    path("rfqs/<str:number>/quote/", views.QuoteView.as_view(), name="portal-quote"),
    path("outlook/", views.OutlookView.as_view(), name="portal-outlook"),
    path("sales-orders/", views.SalesOrderListView.as_view(), name="portal-sales-orders"),
    path("sales-orders/<str:number>/", views.SalesOrderView.as_view(), name="portal-sales-order"),
    path("network/forecast-shares/", views.ForecastShareView.as_view(), name="portal-forecast-shares"),
    path("network/delivery-notices/", views.NetworkNoticeView.as_view(), name="portal-network-notices"),
]
