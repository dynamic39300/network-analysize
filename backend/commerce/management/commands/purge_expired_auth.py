from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone

from commerce.models import DesktopAuthorization, EmailChallenge, RateBucket, UsedRefreshToken


class Command(BaseCommand):
    help = "Remove expired authentication artifacts, leaving financial/support records intact."

    def handle(self, *args, **options):
        cutoff = timezone.now() - timedelta(days=1)
        count = 0
        for query in [
            EmailChallenge.objects.filter(expires_at__lt=cutoff),
            DesktopAuthorization.objects.filter(expires_at__lt=cutoff),
            UsedRefreshToken.objects.filter(expires_at__lt=cutoff),
            RateBucket.objects.filter(starts_at__lt=cutoff),
        ]:
            count += query.delete()[0]
        self.stdout.write(f"Expired authentication artifacts removed: {count}")
