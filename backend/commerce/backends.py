from django.contrib.auth.backends import BaseBackend, ModelBackend

from .models import Account


class AdminPasswordBackend(ModelBackend):
    """A distinct backend identity invalidates legacy mixed-origin session cookies."""

    def authenticate(self, request, username=None, password=None, **kwargs):
        user = super().authenticate(request, username=username, password=password, **kwargs)
        return user if user and user.is_staff else None


class EmailCodeBackend(BaseBackend):
    """Email-code sessions never inherit administrative privileges after promotion.

    The code verifier alone creates these sessions. Password authentication remains
    on the separate AdminPasswordBackend for the administrator login.
    """

    def get_user(self, user_id):
        return Account.objects.filter(
            pk=user_id, is_active=True, is_staff=False, is_superuser=False
        ).first()
