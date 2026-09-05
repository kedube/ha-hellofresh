# Service reference

Every action the integration exposes. All 24 are also listed in Home Assistant under
**Developer tools → Actions**, which renders each field with inline help and is usually the
fastest way to call one by hand — this page adds the response detail and cross-references that
the picker cannot show.

Services marked **returns a response** produce data. Call them with `response_variable` in a
script or automation, or tick *Return response* in Developer tools.

With more than one HelloFresh account configured, pass `config_entry_id` to pick which account a
call targets. With a single account it can be omitted.

## Contents

- [Meals and Market](#meals-and-market)
- [Delivery schedule](#delivery-schedule)
- [Plan and account](#plan-and-account)
- [Food profile](#food-profile)
- [Recipes and favorites](#recipes-and-favorites)
- [Maintenance](#maintenance)

## Meals and Market

Change what is in a box.

### `hellofresh.get_weeks`

**Returns a response.** Delivery weeks with full recipe, selection, market, and order detail (none of which are exposed as entity attributes). Each recipe carries its name, image, description, tags, nutrition, `is_selected`, `selected_quantity`, `course_index`, any surcharge, the variant modifier (`variation_title`, e.g. "2x Bacon"), a `video_url` when HelloFresh published a promo clip for it (only a few meals per week, on past and upcoming weeks alike), `is_favorite` (`true`/`false`, or `null` when the cookbook lookup was skipped or failed), the meal's own per-serving `price`/`price_cents`/`currency` and `price_group` (`premium`/`classic`), `is_sold_out`, the menu `badge` (BESTSELLER, NEW, Premium Picks, …) with HelloFresh's own hex-gated `badge_foreground`/`badge_background` colors, and HelloFresh's `delivered_count`/`last_delivered_week` plus your own `rating` where it has them; each week also includes its `market_items` (HelloFresh Market add-ons), its `menu_categories` (the website's menu sections — This Week's Menu, Health Conscious Menu, Family Menu, … — as `{name, slug, recipe_ids}` rows, present only on weeks with a browsable menu payload), and its matching `order` (tracking, status, carrier, billed total). Optionally filter to one `week_id`. Powers the [Meal planner](dashboard.md#meal-planner-card), [Market](dashboard.md#market-card), and [Schedule](dashboard.md#schedule-card) cards. Each week's summary now includes `benefits` — the wallet promises (weekly discounts such as "$10 off premium meals") HelloFresh will apply to that box — and the `account` payload carries `next_box_discount`, the promise that applies to the next box, as `sensor.next_box_discount` reports it.

### `hellofresh.get_menu_courses`

**Returns a response.** Resolves a delivery week's menu through HelloFresh's **own server-side filter service** — the same call the website's filter panel makes — and returns only the matching recipe ids (`{"week_id", "recipe_ids": [...], "count", "filters"}`) to intersect with that week's `recipes` from `get_weeks`. Pass `week_id` and a `filters` object mapping a filter **group slug** to one or more **option slugs**; both come from the week's `menu_filters` (the menu payload's own filter definitions: `cuisine`, `dish-type`, `exclude-allergens`, `diet`, `main-protein`, `total-cooking-time`, with each option's `slug`). Example: `{"cuisine": ["mediterranean", "world-flavors"], "exclude-allergens": ["nuts"]}`. This is what powers the meal planner card's Cuisine type, Dish type and Ingredients to avoid groups, which the integration cannot answer from recipe tags alone (menu recipes carry no allergen data). Read-only.

### `hellofresh.select_meals`

set the chosen recipes for a week (`week_id` + `recipe_ids`, with an optional `quantities` map of recipe id → servings for doubled portions); writes to the website's own cart endpoint. Selecting more or fewer distinct meals than your plan resizes the box for that week (minimum 2 meals). Optionally **returns a response** `{ "downgraded": <bool> }` — true when HelloFresh accepted the write but silently shrank the box to fit (see the seamless-downgrade note below)

### `hellofresh.select_market_items`

set the HelloFresh Market add-on (extras) selection for a week (`week_id` + a `quantities` map of market item id/sku/index → quantity; 0 removes an item); writes the cart's `extras`, preserving the week's meal selection. Optionally **returns a response** `{ "downgraded": <bool> }` (as above)

### `hellofresh.preview_meal_price`

**Returns a response.** What a hypothetical meal selection *would* cost, without saving it — `grand_total`, `sub_total`, `shipping_amount`, `tax_amount`, `discount_amount`, and per-meal premium `surcharges`. Takes `week_id` + `recipe_ids` (plus an optional `quantities` map). Read-only: nothing is written to your account. Only works for weeks that are still bookable.

## Delivery schedule

Control when boxes arrive, or whether they arrive at all.

### `hellofresh.skip_week`

skip a chosen delivery week so no box ships

### `hellofresh.unskip_week`

restore a previously skipped week

### `hellofresh.reschedule_week`

move a single week's delivery to a different delivery option (one-off)

### `hellofresh.change_delivery_weekday`

change the recurring delivery day (affects all future deliveries)

### `hellofresh.get_delivery_options`

**Returns a response.** The plan's selectable delivery days (weekday, name, price, and which is the current default) — the full delivery-day picker the website uses, a richer superset of the per-week reschedule options. Read-only.

## Plan and account

Read or change the subscription itself.

### `hellofresh.get_account_summary`

**Returns a response.** The account/subscription headline values (status, plan and plan total with its `selected_plan_price_breakdown`, credit, servings, boxes received, address, upcoming/skipped counters, coupon, payment date, preselected flag, holiday notice) in one call — the same values the corresponding sensors report — plus the payment-method health behind `binary_sensor.payment_method_expiring` (`payment_method_expiring`, `payment_method_expired`, `payment_card_type`, `payment_card_provider`, `payment_card_brand`, `payment_card_last4`, `payment_card_expiry`; never the billing address) and the refresh contract (`refresh_interval_minutes`, `delivery_watch_interval_minutes`, `delivery_in_progress`) the cards use to pace their re-fetches. Read-only. Powers the [Subscription card](dashboard.md#subscription-card).

### `hellofresh.get_plan_options`

**Returns a response.** The box sizes you can switch to (meals per week × servings) with prices. Read-only; pairs with `change_plan`.

### `hellofresh.change_plan`

change the recurring box size (`product_handle` from `get_plan_options`). Affects all future boxes and what you are billed.

### `hellofresh.get_plans`

**Returns a response.** The account's plan catalog (product handle, price, status). Read-only.

### `hellofresh.get_presets`

**Returns a response.** The region's menu presets (Chef's Choice, Veggie, Quick & Easy, …) with their handle, name, and description — the human-readable names behind a plan's preset. Read-only.

### `hellofresh.get_spending`

**Returns a response.** Your HelloFresh spending ledger built from the full billing history — `weeks` (per-box delivery date + amount, newest first), `months` (per-month rollup with box count and total), and a running `total` (lifetime spend across past deliveries, with box count). Each week also carries the **realized discount** (`discount`, the billing ledger's coupon lines for that delivery, and its `coupon_code`), months a `discount` rollup and the total a running `discount` — the amounts are already net. Upcoming boxes are flagged and excluded from the running total. Read-only. Powers the [Cost card](dashboard.md#cost-card).

## Food profile

The preferences HelloFresh uses to auto-preselect meals.

### `hellofresh.get_food_profile`

**Returns a response.** The customer's food profile (the preferences HelloFresh uses to auto-preselect meals), a `completion` summary (how many profile fields HelloFresh considers answered, and which are still outstanding), plus the full catalog of selectable options (taste exclusions, dietary preference, liked/disliked cuisines/proteins/flavors/dish-types, nutrition goals, meal types, household size, and goals). Read-only; fetched live from the profile-service. Powers the [Food Profile card](dashboard.md#food-profile-card).

### `hellofresh.set_food_profile`

update the food profile; provide any of `taste`, `household`, or `goals` (only the supplied sections change). Weighted taste fields accept either a list of liked slugs or a `{slug: +100/-100}` map. Returns the saved profile.

## Recipes and favorites

The public recipe catalog and your cookbook.

### `hellofresh.get_recipe_collections`

**Returns a response.** The browsable categories of HelloFresh's public recipe catalog (Chicken Recipes, Carb Smart, Hall of Fame, …), each with a slug, name, and thumbnail. Read-only. Powers the [Recipes card](dashboard.md#recipes-card).

### `hellofresh.get_catalog_recipes`

**Returns a response.** Recipes from the public catalog (~10,000 recipes), optionally within one `collection`, with `limit` (1–200, default 50). Pass `search` to **text-search the whole catalog** instead — every category at once, via HelloFresh's own recipes-service search API (`collection` is ignored then, and no `subcollections` are returned since results span categories). Each recipe carries its name, headline, image, rating, ratings count, prep time, canonical URL, and `is_favorite`. Also returns `subcollections` — that category's child categories (Noodle Recipes → Ramen / Udon / Rice / Soba / Yakisoba), which do **not** appear in `get_recipe_collections`. Pass a child's `path` (e.g. `noodle-recipes/ramen-noodles`), not its bare slug, as the `collection` to browse it. This is browse content shared by all customers — it is **not** tied to your subscription or delivery weeks. Read-only.

### `hellofresh.get_recipe_detail`

**Returns a response.** One recipe's full cooking detail — `ingredients` (each with an amount scaled to the requested `servings`, and flagged when it's a pantry staple you supply rather than something shipped in the box), step-by-step `steps`, `utensils`, `allergens`, `nutrition`, `video_url`, and `card_url` (the printable recipe-card PDF). Works for any recipe id, from a delivery week or the browse catalog. Unlike the catalog listing, this reads a plain HelloFresh API rather than the website, so it does **not** depend on the site's build id. Read-only. Powers the recipe detail view in the [Recipes card](dashboard.md#recipes-card).

### `hellofresh.get_favorites`

**Returns a response.** Your HelloFresh cookbook. Called with no arguments it lists **every** bookmark with full detail (title, headline, image, times, nutrition) — including the ones HelloFresh's own website hides, since its cookbook page only ever renders a 3-item preview while the underlying endpoint reports the true total and pages the rest. Passing `recipe_ids` instead uses HelloFresh's cheaper filter endpoint to answer "which of *these* are bookmarked?", returning ids only — which is how the meal-planner card decorates a week it already has in hand. Read-only.

### `hellofresh.add_favorite`

bookmark a recipe in your cookbook (`recipe_id`). Returns the created favorite (title, image, times, nutrition).

### `hellofresh.remove_favorite`

remove a recipe bookmark (`recipe_id`).

## Maintenance

### `hellofresh.refresh_data`

refresh account data immediately, outside the normal polling interval

## Seamless downgrades

If a meal or Market change is accepted but HelloFresh **silently downsizes the box** to fit (a
"seamless downgrade"), the integration raises a persistent notification so you know the saved
selection is smaller than you asked for. `select_meals` and `select_market_items` also report it
in their `{"downgraded": true}` response, which the [Meal planner](dashboard.md#meal-planner-card) and
[Market](dashboard.md#market-card) cards use to show an inline, dismissable warning on the affected
week.

## Calling these without YAML

Every write service has an interactive equivalent, which is usually easier than composing a call
by hand:

| Instead of calling | Use |
|---|---|
| `select_meals`, `skip_week`, `unskip_week` | [Meal planner card](dashboard.md#meal-planner-card) |
| `select_market_items` | [Market card](dashboard.md#market-card) |
| `get_food_profile`, `set_food_profile` | [Food Profile card](dashboard.md#food-profile-card) |
| `skip_week` for the next editable week | The **Skip next selectable delivery week** switch |

Write actions (meal/Market selection, skip/unskip) use the website's verified endpoints first and
stop with a clear error — raising a Repairs issue — rather than guessing. See
[Current Scope](../README.md#current-scope).

