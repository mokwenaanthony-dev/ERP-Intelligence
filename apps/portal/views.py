"""REST API of the partner portal (Wave 10a, ADR 0019), under ``/api/portal/``.

For suppliers (role SUPPLIER), always about their own documents only:

    GET  me/                                           who you are and which partner you act for
    GET  purchase-orders/                              your released purchase orders
    GET  purchase-orders/<number>/
    POST purchase-orders/<number>/confirm/             {lines: [{item_number, quantity, delivery_date}], note}
    GET  delivery-notices/                             your delivery notices
    POST delivery-notices/                             {reference, order_number, ship_date, arrival_date,
                                                        items: [{item_number, quantity}]}  201 / 200 same again
    GET  rfqs/                                         requests you were invited to, with your own quote
    POST rfqs/<number>/quote/                          {prices: {material: price}, delivery_days, reference}
    GET  outlook/                                      the demand outlook, if purchasing shares it with you

For customers (role CUSTOMER):

    GET  sales-orders/                                 your orders: lines, delivered, invoiced, late
    GET  sales-orders/<number>/

For the company's own users (the network seen from inside):

    GET  network/forecast-shares/?company_code=1000    who sees the outlook
    POST network/forecast-shares/                      {company_code, supplier, active}  purchasing only
    GET  network/delivery-notices/?company_code=1000   what suppliers say is on its way (for receiving)

Another partner's document answers 404, exactly like one that does not exist.
"""
from django.utils import timezone
from rest_framework import serializers, status
from rest_framework.exceptions import NotFound, PermissionDenied
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.accounts.permissions import CustomerUser, PartnerNetworkPermission, PartnerUser, SupplierUser
from apps.masterdata.models import BusinessPartner
from apps.organization.models import CompanyCode

from . import services
from .models import DeliveryNotice, ForecastShare


def _call(func, *args, **kwargs):
    """Run a portal service: not found 404, not shared 403, conflict 409, other broken rules 400."""
    try:
        return func(*args, **kwargs), None
    except services.NotFound as exc:
        raise NotFound(str(exc)) from exc
    except services.NotShared as exc:
        raise PermissionDenied(str(exc)) from exc
    except services.PortalConflict as exc:
        return None, Response({"detail": str(exc)}, status=status.HTTP_409_CONFLICT)
    except services.PostingError as exc:
        raise serializers.ValidationError({"non_field_errors": [str(exc)]}) from exc


def _access(request):
    access, _ = _call(services.access_for, request.user)
    return access


class MeView(APIView):
    permission_classes = [PartnerUser]

    def get(self, request):
        access = _access(request)
        return Response({"username": request.user.username, "kind": access.kind, "partner": access.partner.number,
                         "name": access.partner.name, "company_code": access.company_code.code})


# --- supplier --------------------------------------------------------------------------------------------

class PurchaseOrderListView(APIView):
    permission_classes = [SupplierUser]

    def get(self, request):
        found = services.purchase_orders(_access(request))
        return Response({"count": len(found), "purchase_orders": found})


class PurchaseOrderView(APIView):
    permission_classes = [SupplierUser]

    def get(self, request, number):
        result, _ = _call(services.purchase_order, _access(request), number)
        return Response(result)


class ConfirmLine(serializers.Serializer):
    item_number = serializers.IntegerField(min_value=1)
    quantity = serializers.DecimalField(max_digits=18, decimal_places=3)
    delivery_date = serializers.DateField()


class ConfirmInput(serializers.Serializer):
    lines = ConfirmLine(many=True)
    note = serializers.CharField(max_length=200, required=False, allow_blank=True, default="")


class ConfirmView(APIView):
    permission_classes = [SupplierUser]

    def post(self, request, number):
        given = ConfirmInput(data=request.data)
        given.is_valid(raise_exception=True)
        lines = [services.LineConfirmation(**line) for line in given.validated_data["lines"]]
        result, refused = _call(services.confirm, _access(request), number, lines, user=request.user,
                                note=given.validated_data["note"])
        if refused:
            return refused
        order, changed = result
        return Response({**order, "lines_changed": changed})


class NoticeLine(serializers.Serializer):
    item_number = serializers.IntegerField(min_value=1)
    quantity = serializers.DecimalField(max_digits=18, decimal_places=3)


class NoticeInput(serializers.Serializer):
    reference = serializers.CharField(max_length=35)
    order_number = serializers.CharField(max_length=12)
    ship_date = serializers.DateField()
    arrival_date = serializers.DateField()
    items = NoticeLine(many=True)


class DeliveryNoticeView(APIView):
    permission_classes = [SupplierUser]

    def get(self, request):
        found = services.notices(_access(request))
        return Response({"count": len(found), "delivery_notices": found})

    def post(self, request):
        given = NoticeInput(data=request.data)
        given.is_valid(raise_exception=True)
        data = given.validated_data
        items = {}
        for line in data["items"]:
            if line["item_number"] in items:
                raise serializers.ValidationError({"items": ["Give each line once."]})
            items[line["item_number"]] = line["quantity"]
        result, refused = _call(services.send_notice, _access(request), reference=data["reference"],
                                order_number=data["order_number"], ship_date=data["ship_date"],
                                arrival_date=data["arrival_date"], items=items, user=request.user)
        if refused:
            return refused
        notice, created = result
        return Response(services.notice_view(notice), status=status.HTTP_201_CREATED if created else status.HTTP_200_OK)


class RFQListView(APIView):
    permission_classes = [SupplierUser]

    def get(self, request):
        found = services.rfqs(_access(request))
        return Response({"count": len(found), "rfqs": found})


class QuoteInput(serializers.Serializer):
    prices = serializers.DictField(child=serializers.DecimalField(max_digits=18, decimal_places=2), allow_empty=False)
    delivery_days = serializers.IntegerField(min_value=0)
    reference = serializers.CharField(max_length=35, required=False, allow_blank=True, default="")


class QuoteView(APIView):
    permission_classes = [SupplierUser]

    def post(self, request, number):
        given = QuoteInput(data=request.data)
        given.is_valid(raise_exception=True)
        data = given.validated_data
        result, _ = _call(services.submit_quote, _access(request), number, prices=data["prices"],
                          delivery_days=data["delivery_days"], reference=data["reference"],
                          on=timezone.localdate(), user=request.user)
        return Response(result)


class OutlookView(APIView):
    permission_classes = [SupplierUser]

    def get(self, request):
        months = request.query_params.get("months", "6")
        if not months.isdigit() or not 1 <= int(months) <= 12:
            raise serializers.ValidationError({"months": ["Between 1 and 12."]})
        result, _ = _call(services.outlook, _access(request), on=timezone.localdate(), months=int(months))
        return Response(result)


# --- customer --------------------------------------------------------------------------------------------

class SalesOrderListView(APIView):
    permission_classes = [CustomerUser]

    def get(self, request):
        found = services.sales_orders(_access(request), on=timezone.localdate())
        return Response({"count": len(found), "sales_orders": found})


class SalesOrderView(APIView):
    permission_classes = [CustomerUser]

    def get(self, request, number):
        result, _ = _call(services.sales_order, _access(request), number, on=timezone.localdate())
        return Response(result)


# --- inside the company --------------------------------------------------------------------------------------

class CompanyQuery(serializers.Serializer):
    company_code = serializers.SlugRelatedField(slug_field="code", queryset=CompanyCode.objects.all())


class ShareInput(CompanyQuery):
    supplier = serializers.SlugRelatedField(slug_field="number", queryset=BusinessPartner.objects.all())
    active = serializers.BooleanField()


def _share_view(share: ForecastShare) -> dict:
    return {"supplier": share.supplier.number, "name": share.supplier.name, "active": share.active,
            "agreed_by": share.agreed_by.username, "agreed_at": share.agreed_at, "ended_at": share.ended_at}


class ForecastShareView(APIView):
    permission_classes = [PartnerNetworkPermission]

    def get(self, request):
        query = CompanyQuery(data=request.query_params)
        query.is_valid(raise_exception=True)
        shares = ForecastShare.objects.filter(company_code=query.validated_data["company_code"]).select_related(
            "supplier", "agreed_by").order_by("supplier__number")
        return Response({"forecast_shares": [_share_view(s) for s in shares]})

    def post(self, request):
        given = ShareInput(data=request.data)
        given.is_valid(raise_exception=True)
        data = given.validated_data
        result, _ = _call(services.share, data["company_code"], data["supplier"], active=data["active"],
                          user=request.user)
        share, changed = result
        return Response({**_share_view(share), "changed": changed})


class NetworkNoticeView(APIView):
    permission_classes = [PartnerNetworkPermission]

    def get(self, request):
        query = CompanyQuery(data=request.query_params)
        query.is_valid(raise_exception=True)
        found = [services.notice_view(n) for n in DeliveryNotice.objects.filter(
            company_code=query.validated_data["company_code"]).select_related("supplier", "purchase_order")[:500]]
        return Response({"count": len(found), "delivery_notices": found})
