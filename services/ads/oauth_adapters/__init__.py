"""OAuth Provider Adapters Package"""
from typing import Dict, Type

from services.ads.oauth_adapters.base import BaseOAuthProvider
from services.ads.oauth_adapters.google_ads import GoogleAdsOAuthProvider
from services.ads.oauth_adapters.linkedin_ads import LinkedInAdsOAuthProvider
from services.ads.oauth_adapters.meta_ads import MetaAdsOAuthProvider
from services.ads.oauth_adapters.microsoft_ads import MicrosoftAdsOAuthProvider
from services.ads.oauth_adapters.social_adapters import (
    FacebookPageOAuthProvider,
    InstagramOAuthProvider,
    LinkedInPageOAuthProvider,
    TikTokOAuthProvider,
    XTwitterOAuthProvider,
    YouTubeOAuthProvider,
)

ADAPTER_MAP: Dict[str, Type[BaseOAuthProvider]] = {
    # Paid Ads
    "GOOGLE_ADS": GoogleAdsOAuthProvider,
    "MICROSOFT_ADS": MicrosoftAdsOAuthProvider,
    "META_ADS": MetaAdsOAuthProvider,
    "LINKEDIN_ADS": LinkedInAdsOAuthProvider,
    # Organic Social
    "LINKEDIN_PAGE": LinkedInPageOAuthProvider,
    "X_TWITTER": XTwitterOAuthProvider,
    "INSTAGRAM": InstagramOAuthProvider,
    "FACEBOOK_PAGE": FacebookPageOAuthProvider,
    "YOUTUBE": YouTubeOAuthProvider,
    "TIKTOK": TikTokOAuthProvider,
}


def get_oauth_adapter(provider: str) -> BaseOAuthProvider:
    """Returns an instance of the provider's OAuth adapter."""
    adapter_cls = ADAPTER_MAP.get(provider.upper())
    if not adapter_cls:
        raise ValueError(f"Unsupported OAuth provider: {provider}")
    return adapter_cls()


__all__ = [
    "BaseOAuthProvider",
    "GoogleAdsOAuthProvider",
    "MicrosoftAdsOAuthProvider",
    "MetaAdsOAuthProvider",
    "LinkedInAdsOAuthProvider",
    "LinkedInPageOAuthProvider",
    "XTwitterOAuthProvider",
    "InstagramOAuthProvider",
    "FacebookPageOAuthProvider",
    "YouTubeOAuthProvider",
    "TikTokOAuthProvider",
    "get_oauth_adapter",
]
