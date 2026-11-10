"""JSON output with exact decimals (ADR 0008).

DRF's JSON encoder turns ``Decimal`` into ``float`` when a view returns a plain dict, as every report
does (trial balance, open items, stock, analytics, cost estimates). Floats cannot hold most cent
amounts exactly and drop the scale (``450.00`` becomes ``450.0``). This renderer writes every
``Decimal`` as a string with its exact digits instead, so reports match what serializers already
return (``"450.00"``) and clients can parse amounts as decimals.
"""
from decimal import Decimal

from rest_framework.renderers import JSONRenderer
from rest_framework.utils.encoders import JSONEncoder


class ExactDecimalEncoder(JSONEncoder):
    """DRF's encoder, except that decimals keep every digit and their scale."""

    def default(self, obj):
        if isinstance(obj, Decimal):
            # "f" formatting never switches to exponent notation (str(Decimal("1E+2")) would be "1E+2").
            return format(obj, "f")
        return super().default(obj)


class ExactDecimalJSONRenderer(JSONRenderer):
    """The project's default JSON renderer (``REST_FRAMEWORK["DEFAULT_RENDERER_CLASSES"]``)."""

    encoder_class = ExactDecimalEncoder
