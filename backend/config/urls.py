from django.contrib import admin
from django.urls import path

from commerce import views as v

urlpatterns = [path("admin/", admin.site.urls)]
urlpatterns += [
    path(page, v.page)
    for page in [
        "",
        "features/",
        "pricing/",
        "download/",
        "login/",
        "account/",
        "orders/",
        "support/",
        "privacy/",
        "terms/",
        "desktop/authorize/",
    ]
]
urlpatterns += [
    path("api/v1/" + route, view)
    for route, view in [
        ("config", v.config),
        ("auth/request-code", v.request_code),
        ("auth/verify-code", v.verify_code),
        ("auth/logout", v.web_logout),
        ("me", v.me),
        ("trial/start", v.trial_start),
        ("sessions", v.sessions),
        ("sessions/<uuid:session_id>/revoke", v.revoke_session),
        ("desktop/start", v.desktop_start),
        ("desktop/request", v.desktop_request),
        ("desktop/approve", v.desktop_approve),
        ("desktop/poll", v.desktop_poll),
        ("desktop/refresh", v.desktop_refresh),
        ("desktop/logout", v.desktop_logout),
        ("orders/quote", v.quote),
        ("orders", v.orders),
        ("orders/<uuid:order_id>", v.order_detail),
        ("orders/<uuid:order_id>/sync", v.order_sync),
        ("orders/<uuid:order_id>/simulate", v.order_simulate),
        ("orders/<uuid:order_id>/refund", v.request_refund),
        ("support", v.support),
        ("support/<uuid:ticket_id>/messages", v.support_message),
        ("releases/latest", v.latest),
        ("payments/wechat/notify", v.wechat_notify),
        ("payments/alipay/notify", v.alipay_notify),
    ]
]
urlpatterns += [path("healthz", v.health), path("readyz", v.ready)]
