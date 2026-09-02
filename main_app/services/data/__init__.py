"""Provider selection."""
from django.conf import settings


def get_provider(name: str | None = None, live_feed: bool = False):
    """alpaca when keys exist, else yahoo; 'synthetic' on request."""
    from .synthetic import SyntheticProvider
    from .yahoo import YahooProvider
    name = name or ('alpaca' if settings.ALPACA_ENABLED else 'yahoo')
    if name == 'synthetic':
        return SyntheticProvider()
    if name == 'alpaca':
        if not settings.ALPACA_ENABLED:
            raise RuntimeError('Alpaca keys are not configured (ALPACA_API_KEY / ALPACA_SECRET_KEY)')
        from .alpaca_data import AlpacaDataProvider
        return AlpacaDataProvider(live_feed=live_feed)
    if name == 'yahoo':
        return YahooProvider()
    raise ValueError(f'unknown provider {name!r}')


def provider_status() -> dict:
    return {
        'alpaca': settings.ALPACA_ENABLED,
        'yahoo': True,
        'synthetic': True,
        'default': 'alpaca' if settings.ALPACA_ENABLED else 'yahoo',
    }
