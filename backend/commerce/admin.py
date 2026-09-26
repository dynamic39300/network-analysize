from django import forms
from django.contrib import admin, messages
from django.contrib.auth.admin import UserAdmin
from django.core.exceptions import ValidationError

from .domain import reject_refund
from .errors import APIError
from .models import (
    Account,
    AuditEvent,
    ConsentRecord,
    DesktopSession,
    EntitlementGrant,
    Order,
    Refund,
    Release,
    SupportMessage,
    SupportTicket,
)
from .payments import process_refund, synchronize_order


class ReadOnlyAdmin(admin.ModelAdmin):
    def get_readonly_fields(self, request, obj=None):
        return [field.name for field in self.model._meta.fields]

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(Account)
class AccountAdmin(UserAdmin):
    list_display = ["email", "is_active", "is_staff", "trial_ends_at"]
    readonly_fields = ["id", "trial_started_at", "trial_ends_at"]
    fieldsets = UserAdmin.fieldsets + (
        ("使用权", {"fields": ("id", "trial_started_at", "trial_ends_at")}),
    )
    search_fields = ["email"]


@admin.register(Order)
class OrderAdmin(ReadOnlyAdmin):
    list_display = ["id", "account", "channel", "amount", "status", "created_at"]
    list_filter = ["status", "channel"]
    actions = ["sync_orders"]

    @admin.action(description="从支付渠道核验并补发选中订单", permissions=["change"])
    def sync_orders(self, request, queryset):
        for order in queryset[:50]:
            try:
                synchronize_order(order)
                AuditEvent.objects.create(
                    actor=request.user, action="admin_order_sync", target=str(order.pk)
                )
            except APIError:
                self.message_user(request, "部分订单暂未完成查单，请稍后重试。", messages.WARNING)


@admin.register(EntitlementGrant)
class GrantAdmin(ReadOnlyAdmin):
    list_display = ["order", "account", "starts_at", "ends_at", "revoked_at"]


@admin.register(Refund)
class RefundAdmin(ReadOnlyAdmin):
    list_display = ["id", "order", "status", "created_at"]
    actions = ["approve_refunds", "reject_refunds"]

    def has_process_refund_permission(self, request):
        return request.user.has_perm("commerce.process_refund")

    @admin.action(
        description="批准并向渠道提交退款 / 重试未完成退款", permissions=["process_refund"]
    )
    def approve_refunds(self, request, queryset):
        if not self.has_process_refund_permission(request):
            raise PermissionError
        for refund in queryset.select_related("order")[:20]:
            try:
                process_refund(refund, request.user)
            except APIError:
                self.message_user(request, "部分退款仍需查单，请勿另建退款单。", messages.WARNING)

    @admin.action(
        description="拒绝选中未处理申请（须先通过工单说明原因）", permissions=["process_refund"]
    )
    def reject_refunds(self, request, queryset):
        for refund in queryset.filter(status="requested")[:20]:
            explanation = (
                SupportMessage.objects.filter(ticket__order=refund.order, from_support=True)
                .order_by("-created_at")
                .first()
            )
            if not explanation:
                self.message_user(
                    request, "请先在关联订单工单回复拒绝原因，再拒绝申请。", messages.WARNING
                )
                continue
            if not reject_refund(refund.pk, request.user, explanation.body):
                self.message_user(request, "申请状态已变化，未覆盖进行中的退款。", messages.WARNING)


class MessageInline(admin.TabularInline):
    model = SupportMessage
    fields = ["body", "from_support", "created_at"]
    readonly_fields = ["from_support", "created_at"]
    extra = 1
    can_delete = False

    def has_change_permission(self, request, obj=None):
        return False


@admin.register(SupportTicket)
class TicketAdmin(admin.ModelAdmin):
    list_display = ["id", "account", "subject", "status", "created_at"]
    readonly_fields = ["id", "account", "order", "subject", "created_at"]
    inlines = [MessageInline]

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def save_formset(self, request, form, formset, change):
        for message in formset.save(commit=False):
            if not message.pk:
                message.from_support = True
                message.save()
                AuditEvent.objects.create(
                    actor=request.user, action="support_reply", target=str(message.ticket_id)
                )
        formset.save_m2m()


class ReleaseForm(forms.ModelForm):
    class Meta:
        model = Release
        fields = "__all__"

    def clean(self):
        import re

        data = super().clean()
        if data.get("published") and (
            not data.get("verified_signed_notarized")
            or not str(data.get("url", "")).startswith("https://")
            or not re.fullmatch(r"[a-f0-9]{64}", data.get("sha256", ""))
        ):
            raise ValidationError(
                "发布必须提供 HTTPS 下载、有效 SHA256，并确认正式签名与公证已验证。"
            )
        if not re.fullmatch(r"\d+\.\d+\.\d+", data.get("version", "")):
            raise ValidationError("版本号须为 major.minor.patch。")
        return data


@admin.register(Release)
class ReleaseAdmin(admin.ModelAdmin):
    form = ReleaseForm
    list_display = ["version", "published", "verified_signed_notarized", "created_at"]

    def save_model(self, request, obj, form, change):
        super().save_model(request, obj, form, change)
        AuditEvent.objects.create(
            actor=request.user,
            action="release_registered",
            target=obj.version,
            detail={"published": obj.published, "sha256": obj.sha256},
        )


@admin.register(DesktopSession)
class SessionAdmin(ReadOnlyAdmin):
    list_display = ["account", "label", "created_at", "last_seen_at", "revoked"]
    exclude = ["access_hash", "refresh_hash"]

    def get_readonly_fields(self, request, obj=None):
        return [f for f in super().get_readonly_fields(request, obj) if f not in self.exclude]


@admin.register(AuditEvent)
class AuditAdmin(ReadOnlyAdmin):
    list_display = ["action", "actor", "target", "created_at"]

    def has_change_permission(self, request, obj=None):
        return False


@admin.register(ConsentRecord)
class ConsentAdmin(ReadOnlyAdmin):
    list_display = ["account", "document", "version", "accepted_at"]
