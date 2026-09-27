"""Validated local retention and desktop preferences, never network authority."""
DEFAULT_PREFERENCES = {'history_days': 30, 'history_limit': 1000, 'notifications_enabled': False}


def validate_preferences(value):
    if not isinstance(value, dict) or set(value) != set(DEFAULT_PREFERENCES):
        raise ValueError('Unsupported preference fields')
    if type(value['history_days']) is not int or not 7 <= value['history_days'] <= 90:
        raise ValueError('History retention must be 7 to 90 days')
    if type(value['history_limit']) is not int or not 100 <= value['history_limit'] <= 1000:
        raise ValueError('History limit must be 100 to 1000 completed tasks')
    if type(value['notifications_enabled']) is not bool:
        raise ValueError('Notification preference must be boolean')
    return dict(value)
