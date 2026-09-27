"""Offline account configuration for UI tests, independent of host configuration."""
from relay.commercial.client import CommercialConfig
from relay.desktop_services import DesktopServices


def offline_desktop(*args, **kwargs):
    return DesktopServices(*args, commercial_loader=CommercialConfig, **kwargs)
