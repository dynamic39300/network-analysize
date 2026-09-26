from ipaddress import ip_address

from django.conf import settings
from django.http import JsonResponse


class TrustedProxyMiddleware:
    """Only explicitly trusted last-hop proxies may supply a single overwritten real IP."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        source = request.META.get("REMOTE_ADDR", "")
        request.client_ip = source
        if source in settings.TRUSTED_PROXY_IPS:
            try:
                request.client_ip = str(ip_address(request.headers.get("X-Real-IP", "")))
            except ValueError:
                return JsonResponse(
                    {
                        "error": {
                            "code": "invalid_proxy_header",
                            "message": "网络代理配置无效，请联系支持。",
                        }
                    },
                    status=400,
                )
        return self.get_response(request)


class BearerCSRFMiddleware:
    """CSRF is omitted only after a valid bearer was resolved, never by header presence."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        request.desktop_session = None
        if request.path.startswith("/api/v1/") and request.headers.get("Authorization"):
            from .auth import authenticate_bearer
            from .errors import APIError

            try:
                request.desktop_session = authenticate_bearer(request)
            except APIError as error:
                return JsonResponse(
                    {"error": {"code": error.code, "message": error.message}}, status=error.status
                )
            request._dont_enforce_csrf_checks = True
        return self.get_response(request)


class SecurityHeadersMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        if request.path.startswith("/api/") or request.path in {
            "/desktop/authorize/",
            "/account/",
            "/orders/",
            "/login/",
            "/support/",
        }:
            response["Cache-Control"] = "no-store"
        if not request.path.startswith("/admin/"):
            response["Content-Security-Policy"] = (
                "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self' https://openapi.alipay.com"
            )
        response["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        return response
