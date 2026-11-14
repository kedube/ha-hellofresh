"""Meal and Market selection: the cart write and its builders, box resizing, and changing
how many people one week's box is for.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
import re
from typing import Any

from aiohttp import ClientResponse

from .client_core import (
    _FALLBACK_STATUSES,
    _HTTP_BAD_REQUEST,
    _LOGGER,
    _ClientCore,
    _dict_at,
    _refused_with,
)
from .const import api_country_code
from .models import (
    HelloFreshError,
    HelloFreshInvalidRequestError,
    HelloFreshMarketItem,
    HelloFreshNotImplementedError,
    HelloFreshRecipe,
    HelloFreshSubscription,
    HelloFreshWeek,
)
from .parsers import _seg, box_handle_parts, coerce_int, parse_datetime
from .tls_transport import AuthResponse


class CartClientMixin(_ClientCore):
    """Cart writes for a week's meals and Market add-ons; mixed into HelloFreshClient."""

    async def async_select_meals(
        self,
        week_id: str,
        recipe_ids: list[str],
        quantities: dict[str, int] | None = None,
        market_quantities: dict[str, int] | None = None,
    ) -> bool:
        """Submit meal choices for a week.

        ``recipe_ids`` is the set of chosen recipes. ``quantities`` optionally maps a recipe id
        to how many portions of that meal to order (HelloFresh's per-recipe quantity stepper).
        A recipe left out of the map keeps the portions it has in the box now, and one that
        isn't in the box yet gets one. The box is sized by the TOTAL of the portions, as in
        HelloFresh's box math, where a meal at quantity 2 fills two of the box's meals.

        ``market_quantities`` optionally sets the week's Market add-ons in the SAME cart write,
        with :meth:`async_select_market_items`' semantics (item id/sku/index -> total quantity;
        omitted or zero items are removed). Without it the add-ons in the box are kept.

        The cart write replaces the week's whole selection, so both halves are built from the
        week as HelloFresh has it now, never from the last poll: under the week's lock its menu
        is read again (one request) and the write is built from that. A change made in the app
        since the poll, or a write made just before this one, is therefore kept.

        Returns ``True`` when HelloFresh applied a **seamless downgrade** — it accepted the
        write but silently shrank the box to fit (the cart response's ``hasSeamlessDowngraded``
        flag), so the saved selection is smaller than requested. Returns ``False`` otherwise.
        """
        week = self._get_known_week_or_raise(week_id)
        deduplicated_recipe_ids = list(dict.fromkeys(recipe_ids))
        if not deduplicated_recipe_ids:
            raise HelloFreshError("At least one recipe_id is required")
        quantities = quantities or {}
        for recipe_id, quantity in quantities.items():
            if not isinstance(quantity, int) or quantity < 1:
                raise HelloFreshError(
                    f"Quantity for recipe {recipe_id} must be a positive integer, got {quantity!r}"
                )
        self._warn_about_sold_out_meals(week, deduplicated_recipe_ids)
        subscription = await self._async_get_subscription_for_week(week)

        async with self._week_lock(week):
            menu = await self._async_read_week_for_write(subscription, week)
            meals = self._build_cart_selected_meals(menu, deduplicated_recipe_ids, quantities)
            # A week can be resized to a different box for that delivery: picking more or
            # fewer meals than the base plan swaps the box SKU up or down (see
            # _sku_for_meal_count), and HelloFresh reprices it. The only floor is the smallest
            # box HelloFresh sells, MIN_MEALS_PER_WEEK meals. A meal at quantity 2 takes two of
            # the box's meals, as in HelloFresh's Android app
            # (``DefaultGetSelectedCoursesCountUseCase``, ``CourseBoxLimitRule``).
            total_meals = sum(meal["quantity"] for meal in meals)
            if total_meals < self.MIN_MEALS_PER_WEEK:
                raise HelloFreshError(
                    f"Week {week_id} needs at least {self.MIN_MEALS_PER_WEEK} meals, "
                    f"got {total_meals}"
                )
            # Without market_quantities the add-ons in the box (as just read) go back as they
            # are; with it they are resolved before anything is written, so a bad item fails
            # the whole save rather than leaving the meals saved and the extras not.
            extras = (
                self._build_cart_existing_extras(menu)
                if market_quantities is None
                else self._build_cart_extras(
                    self._resolve_market_selection(menu, market_quantities)
                )
            )
            return await self._async_write_cart(subscription, week, meals, extras)

    def _warn_about_sold_out_meals(self, week: HelloFreshWeek, recipe_ids: Sequence[str]) -> None:
        """Log the requested meals that HelloFresh's menu catalog reports as sold out.

        A warning, never a block. The flag comes from the menus-service catalog, which is the
        anonymous REGIONAL menu: it has not been confirmed to track per-customer availability,
        so a meal could read as sold out here while HelloFresh would still accept it for this
        subscription. Blocking on it would make a legitimate selection impossible with no way
        to override, so HelloFresh stays the authority on its own inventory.
        """
        requested = set(recipe_ids)
        sold_out = {
            recipe.recipe_id: recipe.name
            for recipe in week.recipes
            if recipe.is_sold_out and recipe.recipe_id in requested
        }
        if sold_out:
            names = ", ".join(f"{name} ({rid})" for rid, name in sorted(sold_out.items()))
            _LOGGER.warning(
                "Selecting meals for week %s that HelloFresh's menu catalog reports as "
                "sold out: %s. Submitting anyway — HelloFresh will reject the write if they "
                "are genuinely unavailable",
                week.week_id,
                names,
            )

    async def _async_read_week_for_write(
        self, subscription: HelloFreshSubscription, week: HelloFreshWeek
    ) -> HelloFreshWeek:
        """Read a week's menu again, with its Market catalog, for a cart write built on it."""
        menu = await self._async_fetch_week_menu(subscription, week)
        menu.market_items = self._build_market_items(menu.raw)
        return menu

    async def _async_write_cart(
        self,
        subscription: HelloFreshSubscription,
        week: HelloFreshWeek,
        meals: list[dict[str, int]],
        extras: list[dict[str, Any]],
    ) -> bool:
        """Send the verified cart PUT for a week, then re-read the week it changed.

        Returns the response's seamless-downgrade flag (see :meth:`async_select_meals`).
        """
        cart_update = await self._async_build_cart_update(week, subscription, meals, extras)
        response = await self._async_api_request(
            "PUT",
            cart_update["path"],
            params=cart_update["params"],
            json_payload=cart_update["json_payload"],
            extra_headers={"x-requested-by": "shopping-experience-web"},
        )
        _LOGGER.info("HelloFresh cart saved for week %s", week.week_id)
        downgraded = await self._cart_response_downgraded(response, week.week_id)
        self._clear_price_cache()
        await self._async_refresh_week_after_write(subscription, week)
        return downgraded

    async def _cart_response_downgraded(
        self,
        response: ClientResponse | AuthResponse,
        week_id: str,
    ) -> bool:
        """Return True when a cart write response signals a seamless (silent) box downgrade.

        The ``PUT /gw/v1/carts/{week}`` response is ``{"hasSeamlessDowngraded": bool}``: when
        ``true``, HelloFresh accepted the write but shrank the box to fit rather than rejecting
        it, so the saved selection is smaller than requested. Best-effort — an unreadable or
        unexpected body is treated as no downgrade rather than raising on a successful write.
        """
        json_method = getattr(response, "json", None)
        if not callable(json_method):
            # A transport (or test double) that doesn't expose a readable JSON body — a
            # successful write shouldn't be treated as a downgrade just because we can't read it.
            return False
        try:
            payload = await self._async_response_json(response)
        except HelloFreshError:
            return False
        downgraded = bool(isinstance(payload, dict) and payload.get("hasSeamlessDowngraded"))
        if downgraded:
            _LOGGER.warning(
                "HelloFresh seamlessly downgraded the box for week %s: the saved selection is "
                "smaller than requested",
                week_id,
            )
        return downgraded

    async def async_select_market_items(
        self,
        week_id: str,
        quantities: dict[str, int],
    ) -> bool:
        """Set the week's HelloFresh Market add-on (extras) selection.

        ``quantities`` maps a market item's id/sku/index (as returned by get_weeks) to the
        desired quantity; a quantity of 0 (or omitting an item) removes it. The meals in the
        box are kept. Writes the cart's ``extras`` array via PUT /gw/v1/carts/{week}, built
        like :meth:`async_select_meals` from the week as HelloFresh has it now.

        Returns ``True`` if the cart response signals a seamless box downgrade (see
        :meth:`async_select_meals`); market writes preserve the meal count, so this is rare.
        """
        week = self._get_known_week_or_raise(week_id)
        subscription = await self._async_get_subscription_for_week(week)
        async with self._week_lock(week):
            menu = await self._async_read_week_for_write(subscription, week)
            if not menu.market_items:
                raise HelloFreshError(f"Week {week_id} has no market catalog to select from")
            extras = self._build_cart_extras(self._resolve_market_selection(menu, quantities))
            return await self._async_write_cart(
                subscription, week, self._build_cart_existing_meals(menu), extras
            )

    @staticmethod
    def _resolve_market_selection(
        week: HelloFreshWeek,
        quantities: dict[str, int],
    ) -> list[tuple[HelloFreshMarketItem, int, int]]:
        """Resolve requested Market quantities into (item, one_off, preselected) tuples.

        Keys are a Market item's id, sku or index (as get_weeks returns them); values are the
        desired quantity for the week, checked against ``max_quantity``. Zero or omitted items
        are removed. Shared by the Market write and the combined meals-and-Market write so both
        resolve exactly alike.

        As in HelloFresh's Android app, ``oneOffQuantity`` carries the week's whole quantity and
        ``preselectedQuantity`` the item's unchanged recurring quantity. A recurring item being
        removed for this week stays in the cart at ``oneOffQuantity: 0``; leaving it out would
        not cancel the recurring order.
        """
        by_key: dict[str, HelloFreshMarketItem] = {}
        for candidate in week.market_items:
            for key in (candidate.item_id, candidate.sku, str(candidate.index)):
                if key:
                    by_key.setdefault(str(key), candidate)

        resolved: list[tuple[HelloFreshMarketItem, int, int]] = []
        kept: set[int] = set()
        for raw_key, raw_qty in quantities.items():
            quantity = coerce_int(raw_qty) or 0
            if quantity <= 0:
                continue
            item = by_key.get(str(raw_key))
            if item is None or item.index is None:
                raise HelloFreshError(
                    f"Market item {raw_key!r} is not in week {week.week_id}'s catalog"
                )
            if item.max_quantity is not None and quantity > item.max_quantity:
                raise HelloFreshError(
                    f"Market item {item.name!r} allows at most {item.max_quantity}, got {quantity}"
                )
            resolved.append((item, quantity, item.preselected_quantity or 0))
            kept.add(item.index)
        for item in week.market_items:
            if item.preselected_quantity and item.index is not None and item.index not in kept:
                resolved.append((item, 0, item.preselected_quantity))
        return resolved

    @staticmethod
    def _existing_recipe_quantity(recipe: HelloFreshRecipe) -> int:
        """Servings currently selected for a recipe (defaults to 1 when selected but unknown)."""
        if recipe.selected_quantity and recipe.selected_quantity > 0:
            return recipe.selected_quantity
        return 1

    # HelloFresh box SKUs encode the plan as ``<PREFIX>-<MEALS>-<PEOPLE>-<N>`` (e.g.
    # ``US-CBU-3-2-0`` is 3 meals for 2 people). Selecting a different number of meals than the
    # base plan resizes the box for that week: the cart PUT's ``product-sku`` must carry the
    # matching meal-count digit or the endpoint rejects the write with MEAL_SIZE_MISMATCH. This
    # mirrors the web app, which swaps the digit up (``US-CBU-3-2-0`` → ``US-CBU-4-2-0`` for a 4th
    # meal) OR down (``US-CBU-2-2-0`` for 2 meals) to match the selection; the people digit is
    # unchanged. Both directions are HAR-confirmed against a 3-meal plan. The meal count is the
    # total of the quantities: HelloFresh's Android app sizes the box by it, so a meal at
    # quantity 2 counts twice.
    _BOX_SKU_RE = re.compile(r"^(?P<prefix>.+-)(?P<meals>\d+)(?P<rest>-\d+-\d+)$")

    # Smallest box HelloFresh accepts. 2 meals is the known minimum (and the smallest box the
    # HAR proves works); fewer has no valid box SKU to resize to.
    #
    # There is deliberately NO upper bound. It is tempting to cap this at the largest box in
    # ``product_options``, but that catalog is per-subscription and varies: capture 43 offers meal
    # digits 2-6, while capture 37 offers 1-12 for the same account. A hardcoded ceiling would
    # reject selections that are legitimate for other plans, regions, or accounts. HelloFresh is
    # the authority on which SKUs it sells -- an unsellable size is rejected server-side, which is
    # the same outcome as a local cap but without the false negatives.
    MIN_MEALS_PER_WEEK = 2

    @classmethod
    def _sku_for_meal_count(cls, product_sku: str, meal_count: int) -> str:
        """Return ``product_sku`` with its meal-count segment set to ``meal_count``.

        Resizes the box in either direction (up or down) to match the number of meals selected
        (the total of their quantities), and only rewrites SKUs matching the known
        ``<prefix>-<meals>-<people>-<n>``
        box shape; anything else is returned unchanged so we never corrupt an unexpected format.
        """
        match = cls._BOX_SKU_RE.match(product_sku)
        if match is None:
            return product_sku
        current_meals = int(match.group("meals"))
        # A zero meal-count marks an add-on/charge SKU (e.g. ``US-CHARGE-0-0-0``), not a meal
        # box — never rewrite those.
        if current_meals <= 0 or meal_count == current_meals:
            return product_sku
        # Never resize below the smallest real box. A sub-minimum count means "no meal info to
        # size by" (e.g. a market-only write on a week with no confirmed meals, where the meal
        # list is empty) — keep the base plan's SKU rather than emitting an invalid box.
        if meal_count < cls.MIN_MEALS_PER_WEEK:
            return product_sku
        return f"{match.group('prefix')}{meal_count}{match.group('rest')}"

    async def _async_build_cart_update(
        self,
        week: HelloFreshWeek,
        subscription: HelloFreshSubscription,
        meals: list[dict[str, int]],
        extras: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Assemble the shared ``PUT /gw/v1/carts/{week}`` request for meals + extras.

        Raises HelloFreshNotImplementedError naming what the week lacks when the request can't
        be built: no other write path exists to fall back to.
        """
        customer_id = coerce_int(subscription.account_id)
        cutoff_time = self._format_cart_cutoff_time(
            week.selection_deadline
            or parse_datetime(
                week.raw.get("cutoffDate")
                or week.raw.get("selectionDeadline")
                or week.raw.get("deadline")
            )
        )
        # The cart PUT carries the preference the web app sends; HelloFresh's Android app sends
        # it empty, so an unknown preference no longer abandons the write.
        preference = await self._async_get_subscription_plan_preference(subscription) or ""
        # The WEEK's own box first, as for the menu request (and as the app does): a week resized
        # for that delivery only (other servings or box type) must not be written back as the
        # base plan's box.
        week_product = _dict_at(week.raw, "product")
        product_sku = (
            week_product.get("handle")
            or week_product.get("sku")
            or self._find_first_nested_value(subscription.raw, ("sku",))
        )

        missing = [
            name
            for name, value in (
                ("customer id", customer_id),
                ("meal deadline", cutoff_time),
                ("box", product_sku),
                ("subscription id", week.subscription_id),
            )
            if not value
        ]
        if missing:
            raise HelloFreshNotImplementedError(
                f"Week {week.week_id} can't be saved: HelloFresh did not give its "
                f"{', '.join(missing)}"
            )

        # Resize the box when the selection holds more or fewer meals than the box, so
        # HelloFresh accepts it (and prices the extra meals) instead of failing with
        # MEAL_SIZE_MISMATCH. The meal digit is the total of the meals' quantities.
        product_sku = self._sku_for_meal_count(
            str(product_sku), sum(meal.get("quantity") or 1 for meal in meals)
        )

        return {
            "path": f"/gw/v1/carts/{_seg(week.week_id)}",
            "params": {
                "customer": str(customer_id),
                "cutoff_time": cutoff_time,
                "ignore_addons": "false",
                "preference": str(preference),
                "product-sku": str(product_sku),
                "subscription": str(week.subscription_id),
                "update_quantity": "true",
                "week": week.week_id,
            },
            "json_payload": {
                "extras": extras,
                "meals": meals,
            },
        }

    @staticmethod
    def _build_cart_extras(
        resolved: Sequence[tuple[HelloFreshMarketItem, int, int]],
    ) -> list[dict[str, Any]]:
        """Build the cart ``extras`` payload from (item, one_off_qty, preselected_qty) tuples.

        HAR-confirmed shape: extras are grouped by ``groupType``+``sku``, each group carrying a
        ``selection`` list of ``{index, oneOffQuantity, preselectedQuantity, courses}``::

            [{"groupType": "protein", "sku": "US-APR-0-0-0",
              "selection": [{"index": 10089, "oneOffQuantity": 2,
                             "preselectedQuantity": 0, "courses": []}]}]
        """
        groups: dict[tuple[str | None, str | None], dict[str, Any]] = {}
        for item, one_off, preselected in resolved:
            if item.index is None or (one_off <= 0 and preselected <= 0):
                continue
            key = (item.group_type, item.sku)
            group = groups.get(key)
            if group is None:
                group = {"groupType": item.group_type, "sku": item.sku, "selection": []}
                groups[key] = group
            group["selection"].append(
                {
                    "index": item.index,
                    "oneOffQuantity": one_off,
                    "preselectedQuantity": preselected,
                    # A pairing add-on keeps its link to the meal it was added to.
                    "courses": [{"index": index} for index in item.courses],
                }
            )
        return list(groups.values())

    @staticmethod
    def _build_cart_existing_extras(week: HelloFreshWeek) -> list[dict[str, Any]]:
        """Cart ``extras`` payload for the week's current Market selection (unchanged).

        Used when writing a MEAL change so the existing Market selection is preserved: each
        selected item at its quantity for the week, and each recurring item the customer
        removed for this week still at 0 (see ``_resolve_market_selection``).
        """
        resolved: list[tuple[HelloFreshMarketItem, int, int]] = []
        for item in week.market_items:
            if item.index is None:
                continue
            quantity = (item.selected_quantity or 1) if item.is_selected else 0
            if quantity or item.preselected_quantity:
                resolved.append((item, quantity, item.preselected_quantity or 0))
        return CartClientMixin._build_cart_extras(resolved)

    @staticmethod
    def _build_cart_existing_meals(week: HelloFreshWeek) -> list[dict[str, int]]:
        """Cart ``meals`` entries for the week's currently-selected recipes."""
        meals: list[dict[str, int]] = []
        for recipe in week.recipes:
            if recipe.is_selected and recipe.course_index is not None:
                meals.append(
                    {
                        "index": recipe.course_index,
                        "quantity": CartClientMixin._existing_recipe_quantity(recipe),
                    }
                )
        return meals

    @staticmethod
    def _build_cart_selected_meals(
        menu: HelloFreshWeek,
        recipe_ids: Sequence[str],
        quantities: Mapping[str, int] | None = None,
    ) -> list[dict[str, int]]:
        """Translate recipe ids into the cart's ``meals`` entries, from the week's menu.

        ``menu`` is the week as just read from HelloFresh. A recipe in ``quantities`` gets that
        many portions; one left out keeps the portions it has in the box now, and one not in
        the box yet gets one. An id the menu doesn't have (or has without a course index) is
        refused with all such ids named, before anything is written.
        """
        quantities = quantities or {}
        by_id = {recipe.recipe_id: recipe for recipe in menu.recipes}
        meals: list[dict[str, int]] = []
        unknown: list[str] = []
        for recipe_id in recipe_ids:
            recipe = by_id.get(recipe_id)
            if recipe is None or recipe.course_index is None:
                unknown.append(str(recipe_id))
                continue
            quantity = quantities.get(recipe_id)
            if quantity is None:
                quantity = (
                    CartClientMixin._existing_recipe_quantity(recipe) if recipe.is_selected else 1
                )
            meals.append({"index": recipe.course_index, "quantity": quantity})
        if unknown:
            raise HelloFreshError(
                f"Unknown recipes for week {menu.week_id} (or missing a course index): "
                f"{', '.join(sorted(unknown))}"
            )
        return meals

    @staticmethod
    def _format_cart_cutoff_time(value: datetime | None) -> str | None:
        """Format the menu cutoff timestamp for the cart update endpoint."""
        if value is None:
            return None
        if value.tzinfo is None:
            return value.strftime("%Y-%m-%dT%H:%M:%S")
        return value.isoformat()

    # One week's box changed apart from the plan, as HelloFresh's Android app does it. The
    # website has not been seen sending this (see the API reference's "Resize one week").
    _WEEK_SIZE_PATH = "/gw/carts/{week}/size"
    # A refusal whose text names the route, not something in the request: how a gateway may
    # answer a route it doesn't have with a 400.
    _ROUTE_REFUSAL_RE = re.compile(r"\b(?:endpoint|route|path)\b|/carts\b|/size\b", re.IGNORECASE)

    async def async_change_week_servings(self, week_id: str, servings: int) -> dict[str, Any]:
        """Change how many people ONE week's box is for; the plan and the other weeks stay.

        The box keeps its meal count: the people segment of its handle is replaced
        (``US-CBU-3-2-0`` for 4 people is ``US-CBU-3-4-0``). Refused before anything is written
        (HelloFreshInvalidRequestError) for a week the account doesn't have, a skipped week, a
        week past its deadline, and a size the subscription's box sizes don't offer for that
        meal count. The new box is priced with ``/gw/calculate`` first, as HelloFresh's app
        does, then written with ``PUT /gw/carts/{week}/size``. The website has not been seen
        making that request, so a 404 or 405 (or a 400 naming the route) is raised as
        HelloFreshNotImplementedError, which puts up the write-actions repair.

        The week's box is read from HelloFresh under the week's write lock, so a meal change
        made just before (here or in the app) keeps its meal count. Returns ``{week_id,
        servings, product_handle, price, currency, changed}``: the price in major units, None
        when HelloFresh did not price it, and ``changed`` False when the box was already for
        that many people, in which case nothing is written.
        """
        if not isinstance(servings, int) or isinstance(servings, bool) or servings < 1:
            raise HelloFreshInvalidRequestError(
                f"Servings must be a whole number of people, got {servings!r}"
            )
        if self._last_account_data is None:
            raise HelloFreshError("No HelloFresh account data loaded yet")
        week = self._last_account_data.get_week(week_id)
        if week is None:
            raise HelloFreshInvalidRequestError(f"HelloFresh week not found: {week_id}")
        subscription = await self._async_get_subscription_for_week(week)

        async with self._week_lock(week):
            fresh_weeks, _orders = await self._async_read_account_weeks(subscription)
            current = next(
                (
                    candidate
                    for candidate in fresh_weeks
                    if (candidate.week_id, candidate.subscription_id)
                    == (week.week_id, week.subscription_id)
                ),
                week,
            )
            if current.is_skipped or current.is_paused:
                raise HelloFreshInvalidRequestError(
                    f"Week {week_id} is skipped, so its box can't be changed. Restore the week "
                    "first."
                )
            if not current.is_editable:
                raise HelloFreshInvalidRequestError(
                    f"Week {week_id} can no longer be changed: its deadline has passed or "
                    "HelloFresh has locked it."
                )
            box = self._week_box_handle(current, subscription)
            parts = box_handle_parts(box)
            if box is None or parts is None:
                raise HelloFreshNotImplementedError(
                    f"Week {week_id}'s box can't be resized: HelloFresh did not give it as a "
                    "box handle with a meal count and a number of people."
                )
            prefix, meals, people, recurrency = parts
            target = f"{prefix}-{meals}-{servings}-{recurrency}"

            if people == servings:
                price, currency = await self._async_price_week_box(subscription, current, box)
                return {
                    "week_id": week_id,
                    "servings": servings,
                    "product_handle": box,
                    "price": price,
                    "currency": currency,
                    "changed": False,
                }

            options = await self.async_list_plan_options(subscription.subscription_id)
            offered = self._offered_people(options, prefix, meals)
            if servings not in offered:
                choice = (
                    f" It offers {', '.join(str(count) for count in sorted(offered))}."
                    if offered
                    else ""
                )
                raise HelloFreshInvalidRequestError(
                    f"HelloFresh doesn't offer a box of {meals} meals for {servings} people on "
                    f"this plan.{choice}"
                )

            # Priced first, as the app does before it changes the box; the price is reported,
            # and a box HelloFresh cannot price is still changed.
            price, currency = await self._async_price_week_box(subscription, current, target)
            try:
                await self._async_api_request(
                    "PUT",
                    self._WEEK_SIZE_PATH.format(week=_seg(week.week_id)),
                    params={"subscription": str(week.subscription_id)},
                    json_payload={
                        "productHandle": target,
                        "hfMarketCode": api_country_code(self._country),
                    },
                )
            except HelloFreshError as err:
                if self._route_refused(err):
                    raise HelloFreshNotImplementedError(
                        "HelloFresh doesn't offer changing one week's servings here"
                    ) from err
                raise
            # Week ids and box handles are not account-identifying.
            _LOGGER.info("HelloFresh week %s changed to box %s", week_id, target)
            self._clear_price_cache()
            await self._async_refresh_week_after_write(subscription, week)
            self._show_written_box(week, target, servings)
        return {
            "week_id": week_id,
            "servings": servings,
            "product_handle": target,
            "price": price,
            "currency": currency,
            "changed": True,
        }

    def _week_box_handle(
        self, week: HelloFreshWeek, subscription: HelloFreshSubscription
    ) -> str | None:
        """The handle of a week's own box, else the plan's (as the cart write reads it)."""
        product = _dict_at(week.raw, "product")
        handle = (
            product.get("handle")
            or product.get("sku")
            or self._find_first_nested_value(subscription.raw, ("sku",))
        )
        return str(handle) if handle else None

    @staticmethod
    def _offered_people(options: Sequence[Mapping[str, Any]], prefix: str, meals: int) -> set[int]:
        """The numbers of people the plan's box sizes offer for ``meals`` meals.

        Only the week's own box family counts (the handle's prefix); a size whose handle isn't
        a box handle is judged by its meal count and people alone.
        """
        offered: set[int] = set()
        for option in options:
            parts = box_handle_parts(option.get("handle"))
            if parts is not None and parts[0] != prefix:
                continue
            option_meals = option.get("meals") or (parts[1] if parts else None)
            option_people = option.get("servings") or (parts[2] if parts else None)
            if option_meals == meals and isinstance(option_people, int):
                offered.add(option_people)
        return offered

    async def _async_price_week_box(
        self, subscription: HelloFreshSubscription, week: HelloFreshWeek, handle: str
    ) -> tuple[float | None, str | None]:
        """The price of ``handle`` as the week's box, in major units, and its currency."""
        pricing = await self._async_get_calculate_price(subscription, week, product_handle=handle)
        if pricing is None:
            return None, None
        total = self._extract_total_price(pricing)
        if total is None:
            return None, None
        return round(total, 2), self._extract_currency_code(pricing)

    def _route_refused(self, err: HelloFreshError) -> bool:
        """True when HelloFresh refused the route itself rather than the request it carried."""
        if _refused_with(err, *_FALLBACK_STATUSES):
            return True
        return _refused_with(err, _HTTP_BAD_REQUEST) and bool(
            err.detail and self._ROUTE_REFUSAL_RE.search(err.detail)
        )

    def _show_written_box(self, week: HelloFreshWeek, handle: str, servings: int) -> None:
        """Show the box just written, when HelloFresh's own read does not show it yet.

        As with a new delivery day, HelloFresh's read can lag a write that has committed, and
        a failed re-read publishes nothing. Either way the week is published with the box
        written, so the next get_weeks already has the new number of people.
        """
        data = self._last_account_data
        shown = data.get_week(week.week_id, week.subscription_id) if data is not None else None
        if shown is None or shown.subscription_id != week.subscription_id:
            return
        product = _dict_at(shown.raw, "product")
        if shown.servings == servings and product.get("handle") == handle:
            return
        written = self._detached_week(shown, drop_menu=False)
        specs = _dict_at(product, "specs")
        written.raw["product"] = {**product, "handle": handle, "specs": {**specs, "size": servings}}
        written.servings = servings
        self._publish_week(written, None)
