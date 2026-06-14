"""Backwards-compatible re-export shim — import from submodules directly."""

from .client import HelloFreshClient
from .models import (
    HelloFreshAccountData,
    HelloFreshAuthError,
    HelloFreshCapabilities,
    HelloFreshError,
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
    "HelloFreshError",
    "HelloFreshNotImplementedError",
    "HelloFreshOrder",
    "HelloFreshRecipe",
    "HelloFreshSubscription",
    "HelloFreshWeek",
]
