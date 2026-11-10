"""Sign-out that can be repeated safely (ADR 0010).

simplejwt's logout blacklists the refresh token, but a second call with the same token answers 401
("Token is blacklisted"). A client that retries a logout after a timeout would see an error for an
operation that in fact succeeded. Here a token that is already blacklisted gives the same 200 as the
first call, so logging out twice has the same result as logging out once.
"""
import logging

from rest_framework import status
from rest_framework.response import Response
from rest_framework_simplejwt.exceptions import InvalidToken, TokenError
from rest_framework_simplejwt.token_blacklist.models import BlacklistedToken
from rest_framework_simplejwt.tokens import RefreshToken
from rest_framework_simplejwt.views import TokenBlacklistView

logger = logging.getLogger(__name__)


def _already_blacklisted(raw_token) -> bool:
    try:
        jti = RefreshToken(raw_token, verify=False)["jti"]
    except (TokenError, KeyError, TypeError):
        return False
    return BlacklistedToken.objects.filter(token__jti=jti).exists()


class LogoutView(TokenBlacklistView):
    """POST /api/auth/logout/ {"refresh": ...}: blacklist the refresh token; repeating it is harmless."""

    def post(self, request, *args, **kwargs):
        try:
            return super().post(request, *args, **kwargs)
        except InvalidToken:
            if _already_blacklisted(request.data.get("refresh")):
                logger.info("Logout repeated for a token that is already blacklisted")
                return Response({}, status=status.HTTP_200_OK)
            raise
