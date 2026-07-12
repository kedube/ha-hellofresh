"""Backwards-compatible re-export shim — import from submodules directly."""

from .client import HelloFreshClient
from .models import (
    HelloFreshAccountData,
    HelloFreshAuthError,
    HelloFreshCapabilities,
    HelloFreshDeliveryOption,
    HelloFreshError,
    HelloFreshFoodProfile,
    HelloFreshFoodProfileOptions,
    HelloFreshMarketItem,
    HelloFreshNotImplementedError,
    HelloFreshOrder,
    HelloFreshRecipe,
    HelloFreshSubscription,
    HelloFreshWeek,
)

__all__ = [
    "HelloFreshAccountData",
    "HelloFreshAuthError",
    "HelloFreshCapabilities",
    "HelloFreshClient",
    "HelloFreshDeliveryOption",
    "HelloFreshError",
    "HelloFreshFoodProfile",
    "HelloFreshFoodProfileOptions",
    "HelloFreshMarketItem",
    "HelloFreshNotImplementedError",
    "HelloFreshOrder",
    "HelloFreshRecipe",
    "HelloFreshSubscription",
    "HelloFreshWeek",
]
