"""The food profile: the taste and household preferences behind HelloFresh's automatic
meal pre-selection.
"""

from __future__ import annotations

from typing import Any

from .client_core import _LOGGER, _ClientCore
from .const import api_country_code
from .models import (
    HelloFreshError,
    HelloFreshFoodProfile,
    HelloFreshFoodProfileOptions,
    HelloFreshProfileCompletion,
)


class ProfileClientMixin(_ClientCore):
    """Food-profile reads and writes; mixed into HelloFreshClient."""

    # ---- Food profile (auto-preselection preferences) ------------------------
    #
    # HelloFresh drives its automatic meal pre-selection from a per-customer "food profile"
    # (taste exclusions, dietary preference, liked/disliked cuisines/proteins/etc., household
    # size, goals). The web app reads it from
    #   GET  /gw/profile-service/v2/customers/me/profile
    # the full catalog of choices from
    #   GET  /gw/profile-service/v2/profile/options
    # and saves changes with
    #   PATCH /gw/profile-service/v2/customers/me/profile   (HAR-verified, US).
    # All three take the brand/exclusion/regionCode query params the web app sends.

    _PROFILE_OPTIONS_PATH = "/gw/profile-service/v2/profile/options"
    _PROFILE_PATH = "/gw/profile-service/v2/customers/me/profile"
    _PROFILE_COMPLETION_PATH = "/gw/profile-service/v2/customers/me/profile/completion"

    def _profile_params(self, *, source: str | None = None) -> dict[str, str]:
        params = {
            "brand": "BRAND_HELLOFRESH",
            "exclusion": "v2",
            "regionCode": api_country_code(self._country),
        }
        if source:
            params["source"] = source
        return params

    async def async_get_food_profile_options(self) -> HelloFreshFoodProfileOptions:
        """Return the full catalog of selectable food-profile options."""
        payload = await self._async_get_json(
            self._PROFILE_OPTIONS_PATH, self._profile_params(), trace_category="profile_attempts"
        )
        return HelloFreshFoodProfileOptions.from_api(payload)

    async def async_get_food_profile(self) -> HelloFreshFoodProfile:
        """Return the customer's current food profile (auto-preselection preferences)."""
        payload = await self._async_get_json(
            self._PROFILE_PATH, self._profile_params(), trace_category="profile_attempts"
        )
        return HelloFreshFoodProfile.from_api(payload)

    async def async_get_food_profile_completion(self) -> HelloFreshProfileCompletion | None:
        """Return how complete the food profile is, or None when unavailable.

        Best-effort: this decorates the profile card with a progress figure, so a failure
        should degrade to "no progress shown" rather than break the whole profile fetch.
        """
        payload = await self._async_get_json(
            self._PROFILE_COMPLETION_PATH,
            self._profile_params(),
            trace_category="profile_attempts",
            optional=True,
        )
        return HelloFreshProfileCompletion.from_api(payload) if payload is not None else None

    async def async_update_food_profile(self, changes: dict[str, Any]) -> HelloFreshFoodProfile:
        """Update the customer's food profile and return the saved result.

        ``changes`` is a partial ``{taste, household, goals}`` mapping (only the sections you
        want to change). Weighted taste fields may be given as a list of liked slugs or a
        ``{slug: weight}`` map; both are normalized to the canonical +/-100 form.
        """
        patch = HelloFreshFoodProfile.build_patch(changes)
        if not patch:
            raise HelloFreshError("No food-profile changes were provided")
        response = await self._async_api_request(
            "PATCH",
            self._PROFILE_PATH,
            params=self._profile_params(source="food-profile"),
            json_payload=patch,
        )
        _LOGGER.info("HelloFresh food-profile update succeeded")
        payload = await self._async_response_json(response)
        if not isinstance(payload, dict):
            raise HelloFreshError(
                "HelloFresh saved the food profile but did not answer with the saved profile"
            )
        return HelloFreshFoodProfile.from_api(payload)
