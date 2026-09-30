// Deterministic, date-relative fixture data + a fake `hass` for the unified card harness.
// Shapes follow the integration's service responses (get_weeks, get_account_summary, ...).

const DISHES = [
  ["Prep & Bake Pistachio Salmon", "with Lemony Lettuce Wraps & Dill Sauce", "Seafood", ["High Protein", "Calorie Smart"], 30, 540, 38],
  ["Mozzarella & Herb Chicken", "with Roasted Carrots & Buttery Couscous", "Poultry", ["High Protein", "Fiber Powered"], 35, 760, 53],
  ["XL Beefy Lasagna Soup", "2x the portions for family dinner or leftovers", "Beef", ["Low Added Sugar"], 40, 570, 29],
  ["Garden Spinach Ricotta Ravioli", "with Zucchini Ribbons, Tomato & Creamy Lemon Sauce", "Veggie", ["Under 650 Calories", "Veggie"], 20, 530, 19],
  ["Surf 'n' Turf with Lemon-Thyme Sauce", "plus Zesty Roasted Asparagus & Crispy Potatoes", "Beef", ["Gluten-Free Friendly"], 45, 820, 55],
  ["Chimichurri Steak & Shrimp-Corn Salad", "with Roasted Potatoes, Broccoli & Feta", "Beef", ["High Protein"], 40, 1100, 63],
  ["Kimchi Shrimp & Polenta Grits", "with Green Pepper, Corn & Parmesan", "Seafood", ["Sodium Smart"], 25, 530, 29],
  ["Salt-Cured Cod Cakes & Feta Salad", "with Tzatziki & Cucumber-Mint Lemonade", "Seafood", ["High Protein"], 35, 970, 36],
  ["Korean-Style Hot Honey Chicken Wings", "Enjoy a multi-dish culinary tour of a new destination!", "Poultry", [], 45, 1500, 69],
  ["Prosciutto-Wrapped Chicken", "with Truffled Chive Mashed Potatoes & Lemony Broccoli", "Pork", ["High Protein", "Fiber Powered"], 35, 810, 51],
  ["Organic Chicken Chimichurri Grain Bowls", "with Arugula, Tomato, Cucumber & Edamame", "Poultry", ["Fiber Powered", "Organic Protein"], 30, 680, 51],
  ["Creamy Lemon-Basil Pork Sausage Rigatoni", "with Zucchini", "Pork", ["Under 30 Minutes"], 25, 980, 33],
  ["Shawarma-Spiced Braised Chickpeas & Kale", "with Sweet Potato, Harissa Aioli & Golden Raisins", "Veggie", ["Veggie", "Gluten-Free Friendly"], 30, 920, 32],
  ["Sesame Soy Beef Bowls", "with Shredded Carrots, Buttery Rice & Sriracha Mayo", "Beef", ["High Protein"], 20, 810, 32],
  ["Vegan Mushroom French Dip Sandwiches", "with Arugula, Herby Potatoes, Vegan Aioli & Au Jus", "Veggie", ["Veggie"], 30, 810, 10],
  ["Honey-Miso Sweet Potato 'Shroom Jumble", "over Lime Rice with Zucchini & Creamy Chili Sauce", "Veggie", ["Veggie", "Sodium Smart"], 35, 750, 12],
  ["Herbed Chicken with Roasted Asparagus", "plus Prosciutto-Topped Mashed Potatoes & Pan Sauce", "Poultry", ["High Protein", "Gluten-Free Friendly"], 35, 780, 53],
  ["Smashed Black Bean Tostadas", "with Green Pepper, Pico de Gallo & Lime Crema", "Veggie", ["Veggie", "Fiber Powered"], 25, 880, 28],
  ["Uruguayan-Style Churrasco Steaks", "with Chimichurri, Roasted Potatoes & Corn Salad", "Beef", ["High Protein"], 40, 850, 35],
  ["Spicy Sri Lankan-Inspired Butter Shrimp", "with Bell Pepper, Red Onion, Chili Flakes & Rice", "Seafood", ["Under 650 Calories"], 30, 930, 29],
  ["Greek Diner Spaghetti with Feta", "Topped with Spinach, Tomato, Scallions & Dill", "Veggie", ["Veggie", "Under 30 Minutes"], 20, 550, 17],
  ["French-Style Dijon-Crusted Pork Chops", "with a main, sides and appetizers", "Pork", ["High Protein"], 40, 1140, 46],
  ["Spicy Tunisian Bulgur Bowls", "with Zucchini, Carrots, Chermoula & Creamy Lemon Sauce", "Veggie", ["Veggie", "Fiber Powered", "Sodium Smart"], 30, 590, 12],
  ["Honey Dijon Dill Trout", "with Shingled Potatoes & Roasted Brussels Sprouts", "Seafood", ["High Protein", "Gluten-Free Friendly", "Sodium Smart"], 35, 810, 36],
  ["Chicken Milanese & Creamy Pesto Penne", "Enjoy a multi-dish culinary tour of a new destination!", "Poultry", [], 40, 1370, 66],
  ["Romesco Organic Chicken & Pearl Couscous", "with Asparagus, Grape Tomatoes & Almonds", "Poultry", ["Fiber Powered", "Low Added Sugar", "Organic Protein"], 30, 680, 51],
  ["Teriyaki Turkey Meatballs", "with Garlicky Green Beans & Jasmine Rice", "Poultry", ["Carb Smart"], 25, 690, 34],
  ["Crispy Cheddar Chicken", "with Creamy Mashed Potatoes & Roasted Broccoli", "Poultry", ["High Protein"], 30, 830, 48],
  ["One-Pan Tex-Mex Pork Tacos", "with Pickled Onion, Tomato Salsa & Cilantro", "Pork", ["Under 30 Minutes"], 20, 720, 30],
  ["Thai Coconut Curry Shrimp", "with Jasmine Rice, Green Beans & Peanuts", "Seafood", ["Carb Smart", "GLP-1 Support"], 25, 640, 31],
  ["Lamb Kofta Flatbreads", "with Tzatziki, Cucumber & Mint", "Lamb", ["High Protein"], 30, 840, 38],
  ["Roasted Red Pepper Tortellini", "with Spinach, Parmesan & Crispy Garlic Breadcrumbs", "Veggie", ["Veggie"], 20, 740, 24],
  ["Cajun Salmon & Creamy Grits", "with Charred Corn & Scallions", "Seafood", ["High Protein", "Gluten-Free Friendly"], 30, 780, 42],
  ["BBQ Pulled Pork Sliders", "with Crunchy Slaw & Potato Wedges", "Pork", [], 35, 1010, 39],
  ["Firecracker Meatballs", "with Roasted Green Beans & Jasmine Rice", "Beef", ["Bestseller"], 30, 820, 34],
  ["Mediterranean Chicken Orzo", "with Tomatoes, Olives, Feta & Lemon", "Poultry", ["Under 650 Calories", "Carb Smart"], 25, 620, 44],
  ["Pork Carnitas Burrito Bowls", "with Cilantro-Lime Rice, Black Beans & Queso", "Pork", ["High Protein"], 30, 930, 41],
  ["Miso Butter Chicken Ramen", "with Bok Choy, Soft-Boiled Egg & Scallions", "Poultry", ["New"], 30, 760, 45],
  ["Balsamic Fig Pork Tenderloin", "with Roasted Sweet Potatoes & Arugula Salad", "Pork", ["GLP-1 Support", "Calorie Smart"], 35, 610, 40],
  ["Cheesy Beef Enchiladas", "with Red Sauce, Sour Cream & Cilantro", "Beef", ["Bestseller"], 35, 990, 43],
  ["Lemon Garlic Shrimp Linguine", "with Zucchini, Chili Flakes & Parmesan", "Seafood", ["Under 30 Minutes"], 20, 690, 32],
  ["Sweet Chili Tofu Stir-Fry", "with Snap Peas, Peppers & Brown Rice", "Veggie", ["Veggie", "Low Added Sugar"], 25, 610, 21],
  ["Ancho Chicken Tacos", "with Charred Corn Salsa & Chipotle Crema", "Poultry", ["Calorie Smart"], 25, 640, 36],
  ["Steak Frites with Garlic Herb Butter", "plus a Crisp Side Salad", "Beef", ["New", "Premium"], 35, 920, 44],
];

const BADGES = [
  { badge: "BESTSELLER", badge_background: "#FFC107", badge_foreground: "#1F1F1F" },
  { badge: "NEW", badge_background: "#067A46", badge_foreground: "#FFFFFF" },
  { badge: "Premium Picks", badge_background: "#242424", badge_foreground: "#FFFFFF" },
  { badge: "20 Min or Less", badge_background: "#2F55D4", badge_foreground: "#FFFFFF" },
  { badge: "Global Fusion", badge_background: "#FFE082", badge_foreground: "#5D4037" },
];

const MARKET = [
  ["Garlic Bread", "Ready in a flash! | 2-4 Servings", "appetizer", 4.99, 230],
  ["Pork & Shiitake Gyoza", "Get ready to catch fillings! | 2-3 Servings", "appetizer", 5.99, 200],
  ["Guacamole", "Diptastic! | 8 oz", "appetizer", 5.99, 40],
  ["Southwest Chicken & Kale Caesar Salad", "with Charred Corn, Cotija Cheese, Cilantro & Lime", "appetizer", 9.99, 460],
  ["Chocolate Lava Cakes", "Molten centers | 2 cakes", "dessert", 6.99, 380],
  ["Salted Caramel Cookies", "Bake-at-home | 6 cookies", "dessert", 5.49, 210],
  ["Churro Bites", "with Cinnamon Sugar & Chocolate Dip", "dessert", 4.99, 330],
  ["Chicken Breasts", "Boneless, skinless | 2 x 10 oz", "protein", 11.99, 220],
  ["Salmon Fillets", "Skin-on | 2 x 6 oz", "protein", 14.99, 350],
  ["Ground Beef", "85% lean | 16 oz", "protein", 8.99, 510],
  ["Buttermilk Pancake Kit", "Fluffy weekend stack | 4 Servings", "breakfast", 5.99, 310],
  ["Egg Bites", "Spinach & Feta | 4 bites", "breakfast", 6.49, 180],
  ["Mac & Cheese", "Creamy stovetop classic | 4 Servings", "sides", 5.99, 420],
  ["Roasted Garlic Mashed Potatoes", "Rich & buttery | 4 Servings", "sides", 4.99, 260],
  ["Scrub Daddy x HelloFresh Sponge", "Special edition lime-shaped Scrub Mommy", "lowprices", 4.99, null],
];

const hash = (s) => {
  let h = 2166136261;
  for (const c of String(s)) h = Math.imul(h ^ c.charCodeAt(0), 16777619);
  return Math.abs(h);
};
const rid = (i) => `69a9b548e566b4ce${i.toString(16).padStart(8, "0")}`;

function pad(n) {
  return String(n).padStart(2, "0");
}
function iso(d) {
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
}
function isoWeek(d) {
  const t = new Date(Date.UTC(d.getFullYear(), d.getMonth(), d.getDate()));
  const day = t.getUTCDay() || 7;
  t.setUTCDate(t.getUTCDate() + 4 - day);
  const y0 = new Date(Date.UTC(t.getUTCFullYear(), 0, 1));
  const wk = Math.ceil(((t - y0) / 86400000 + 1) / 7);
  return `${t.getUTCFullYear()}-W${pad(wk)}`;
}

export function buildFixtures(origin, { scenario = "default" } = {}) {
  const now = new Date();
  const today = new Date(now);
  today.setHours(0, 0, 0, 0);
  // Next Monday on/after today.
  const nextMon = new Date(today);
  nextMon.setDate(nextMon.getDate() + ((8 - nextMon.getDay()) % 7));
  const mondays = [];
  for (let i = -10; i <= 4; i += 1) {
    const d = new Date(nextMon);
    d.setDate(d.getDate() + i * 7);
    mondays.push(d);
  }
  const img = (seed) => `${origin}/img/${encodeURIComponent(seed)}.svg`;
  const grace = 2;

  const recipeFor = (dishIdx, weekIdx, course, extra = {}) => {
    const [name, description, preference, tags, mins, kcal, protein] = DISHES[dishIdx % DISHES.length];
    const h = hash(`${name}${weekIdx}`);
    const badge = h % 5 === 0 ? BADGES[h % BADGES.length] : {};
    const premium = h % 7 === 0;
    return {
      recipe_id: rid(dishIdx * 100 + (weekIdx % 3)),
      name,
      description,
      preference,
      is_selected: false,
      selected_quantity: null,
      course_index: course,
      image_url: img(`r${dishIdx}`),
      video_url: h % 9 === 0 ? `${origin}/video/clip.mp4` : null,
      tags: [...tags, ...(h % 11 === 0 ? ["Bestseller"] : [])],
      prep_time_minutes: mins,
      total_time_minutes: Math.round(mins / 3),
      calories_kcal: kcal,
      protein_g: protein,
      surcharge_label: premium ? `+${(4 + (h % 6)).toFixed(0)}.99/serving` : null,
      surcharge_cents: premium ? 499 : null,
      price: premium ? 17.98 : 11.99,
      price_cents: premium ? 1798 : 1199,
      currency: "USD",
      price_group: premium ? "premium" : "classic",
      variation_title: null,
      variation_group: null,
      is_sold_out: h % 29 === 0,
      is_favorite: h % 6 === 0,
      delivered_count: h % 8 === 0 ? 1 + (h % 3) : null,
      last_delivered_week: h % 8 === 0 ? isoWeek(mondays[3]) : null,
      rating: h % 13 === 0 ? 4 : null,
      rating_scale: 5,
      ...badge,
      ...extra,
    };
  };

  // W42-shaped customization options (HAR 56): the base's label, then each option with its
  // surcharge, the protein it switches to (if any) and its ingredient photo.
  const OPTION_SETS = [
    { base: "No Change", options: [
      ["2x Broccoli", 0.99, null, "Broccoli"], ["2x Chicken Cutlets", 2.49, null, "Chicken"],
      ["Salmon", null, "Seafood", "Salmon"], ["2x Salmon", 6.99, "Seafood", "Salmon"],
      ["Green Beans", null, null, "Green Beans"], ["Asparagus", null, null, "Asparagus"],
      ["Organic Chicken Cutlets", 1.49, null, "Organic Chicken"], ["2x Organic Chicken Cutlets", 3.99, null, "Organic Chicken"],
    ] },
    { base: "Ground Beef", options: [
      ["2x Ground Beef", 2.49, null, "Ground Beef"], ["Ground Turkey", null, "Poultry", "Turkey"], ["2x Ground Turkey", 2.49, "Poultry", "Turkey"],
      // Like W42's tofu ramen: no protein in the menu; HelloFresh's filter service calls it Veggie.
      ["2x Tofu", null, "", "Tofu"],
    ] },
    { base: "No Protein", options: [
      ["Added Bacon", 2.99, "Pork", "Bacon"], ["Added 2x Bacon", 4.99, "Pork", "Bacon"],
      ["Added Chopped Chicken Breast", 2.99, "Poultry", "Chicken"], ["Added Shrimp", 3.99, "Seafood", "Shrimp"],
    ] },
  ];

  const menuFor = (weekIdx) => {
    const list = [];
    const options = [];
    let course = 1;
    let optCourse = 200;
    for (let i = 0; i < 40; i += 1) {
      const dish = (weekIdx * 7 + i) % DISHES.length;
      const r = recipeFor(dish, weekIdx, course);
      list.push(r);
      if (i % 4 === 1) {
        const set = OPTION_SETS[(i >> 2) % OPTION_SETS.length];
        r.variation_group = course;
        r.variation_default_title = set.base;
        set.options.forEach(([title, surcharge, pref, ing], order) => {
          optCourse += 1;
          const name = title.startsWith("Added ")
            ? `${r.name} + ${title.slice(6)}`
            : pref !== null
              ? `${r.name} with ${title}`
              : r.name;
          options.push({
            ...recipeFor(dish, weekIdx, optCourse),
            recipe_id: rid(dish * 100 + 40 + order + (weekIdx % 3) * 10),
            name,
            variation_title: title,
            variation_group: course,
            variation_order: order,
            variation_image_url: img(`ing-${ing}`),
            preference: pref === "" ? null : pref || r.preference,
            ...(pref === "" ? { server_protein: "Veggie" } : {}),
            surcharge_label: surcharge ? `+${surcharge.toFixed(2)}/serving` : null,
            surcharge_cents: surcharge ? Math.round(surcharge * 100) : null,
            price: surcharge ? Number((11.99 + surcharge).toFixed(2)) : 11.99,
            is_sold_out: order === 6,
          });
        });
      }
      course += 1;
    }
    // Real menus list every option after the base dishes, under higher course indexes.
    return [...list, ...options];
  };

  const marketFor = (weekIdx) =>
    MARKET.map(([name, description, group, price, kcal], i) => ({
      item_id: `m${i + 1}`,
      recipe_id: kcal ? rid(9000 + i) : null,
      name,
      index: 10000 + i,
      sku: `US-${group.toUpperCase()}-${i}`,
      group_type: group,
      image_url: img(`m${i}`),
      description,
      calories_kcal: kcal,
      price,
      price_cents: Math.round(price * 100),
      currency: "USD",
      max_quantity: 6,
      is_selected: false,
      selected_quantity: 0,
      preselected_quantity: 0,
      is_sold_out: (weekIdx + i) % 13 === 0,
    }));

  const categories = (recipes) => [
    { name: "This Week's Menu", slug: "this-weeks-menu", recipe_ids: recipes.slice(0, 24).map((r) => r.recipe_id) },
    { name: "Health Conscious Menu", slug: "health-conscious", recipe_ids: recipes.filter((r) => r.calories_kcal < 700).map((r) => r.recipe_id) },
    { name: "Family Menu", slug: "family", recipe_ids: recipes.slice(10, 22).map((r) => r.recipe_id) },
    { name: "Premium", slug: "premium", recipe_ids: recipes.filter((r) => r.price_group === "premium").map((r) => r.recipe_id) },
  ];
  const menuFilters = [
    { name: "Main protein", slug: "main-protein", choice: "MULTI-OR", options: [
      { name: "Beef", slug: "beef" }, { name: "Poultry", slug: "poultry" }, { name: "Pork", slug: "pork" },
      { name: "Seafood", slug: "fish-seafood" }, { name: "Veggie", slug: "vegetarian" } ] },
    { name: "Cuisine type", slug: "cuisine", choice: "MULTI-OR", options: [
      { name: "Classic American", slug: "american" }, { name: "Italian", slug: "italian" }, { name: "Mediterranean", slug: "mediterranean" },
      { name: "East Asian", slug: "east-asian" }, { name: "Latin American", slug: "latin-american" } ] },
    { name: "Dish type", slug: "dish-type", choice: "MULTI-OR", options: [
      { name: "Bowls", slug: "bowls" }, { name: "Handhelds", slug: "handhelds" }, { name: "Pasta & Noodles", slug: "pasta" }, { name: "Classic Plates", slug: "plates" } ] },
    { name: "Ingredients to avoid", slug: "exclude-allergens", choice: "MULTI-AND", options: [
      { name: "Milk", slug: "milk" }, { name: "Wheat", slug: "wheat" }, { name: "Nuts", slug: "nuts" }, { name: "Spicy", slug: "spicy" } ] },
  ];

  const weeks = [];
  mondays.forEach((d, idx) => {
    const weekIdx = idx - 10; // 0 = next Monday
    const week_id = isoWeek(d);
    const deadline = new Date(d);
    deadline.setDate(deadline.getDate() - 4);
    deadline.setHours(2, 59, 0, 0);
    const past = d < today;
    const isToday = d.getTime() === today.getTime();
    const withinGrace = past && d >= new Date(today.getTime() - grace * 7 * 86400000);
    const editable = !past && !isToday && deadline > now;
    const week = {
      week_id,
      display_name: "Classic Box",
      subscription_id: "sub-1",
      delivery_date: iso(d),
      delivered_at: null,
      selection_deadline: deadline.toISOString(),
      status: "RUNNING",
      meals_required: 3,
      meals_selected: 3,
      is_skipped: false,
      needs_selection: false,
      meals_preselected: false,
      auto_picked: false,
      is_editable: editable,
      slot_label: "Mondays: 8AM - 8PM",
      allowed_actions: past ? {} : { mealSwap: editable },
      available_one_off_options: editable
        ? [0, 1, 2].map((k) => {
            const o = new Date(d);
            o.setDate(o.getDate() + k);
            return { handle: ["mon-8-20", "tue-8-20", "wed-8-20"][k], delivery_date: iso(o) };
          })
        : [],
      benefits: [],
      holiday_message: null,
      holiday_delivery_date: null,
      recipes: [],
      market_items: [],
      menu_categories: [],
      menu_filters: [],
      order: null,
    };
    const history = past && !withinGrace;
    if (weekIdx >= 4) {
      // Beyond the published menus: an empty scheduling shell (should be hidden).
      weeks.push(week);
      return;
    }
    if (history) {
      week.recipes = [0, 1, 2].map((k) => ({
        ...recipeFor((idx * 5 + k * 3) % DISHES.length, idx, null),
        is_selected: true,
        selected_quantity: 1,
        variation_title: k === 1 ? "2x Chicken Cutlets" : null,
      }));
      week.market_items = idx % 3 === 0 ? [{ ...marketFor(idx)[0], group_type: null, is_selected: true, selected_quantity: 1 }] : [];
    } else {
      week.recipes = menuFor(idx);
      week.menu_categories = past ? [] : categories(week.recipes);
      week.menu_filters = past ? [] : menuFilters;
      week.market_items = marketFor(idx);
      // Selected meals: two dishes as written and one customized (its "2x Chicken Cutlets"-style
      // option), which the dish's tile must show.
      const option = week.recipes.find((r) => r.variation_group === 2 && r.variation_order === 1);
      for (const r of [week.recipes[0], option, week.recipes[6]].filter(Boolean)) {
        r.is_selected = true;
        r.selected_quantity = 1;
      }
    }
    // Orders.
    const billed = 84.93 + ((idx * 7) % 20);
    week.order = {
      order_id: `${week_id}`,
      week_id,
      status: past ? "DELIVERED" : "PENDING",
      carrier: past || isToday ? "Veho" : null,
      tracking_number: past || isToday ? `HF0100004${3500000 + idx}` : null,
      tracking_url: past || isToday ? `https://track.shipveho.com/#/trackingId/HF01000043507${idx}` : null,
      tracking_status: past ? "delivered" : null,
      tracking_status_detail: past ? "delivered" : null,
      tracking_events: [],
      delivery_photo_urls: [],
      delivery_signed_by: null,
      billed_total_price: past ? Number(billed.toFixed(2)) : null,
      billed_total_currency: "USD",
      total_price: past ? null : 104.91,
      currency: "USD",
      slot_label: "Mondays: 8AM - 8PM",
    };
    if (past) {
      week.status = "DELIVERED";
      const at = new Date(d);
      at.setHours(17, 42, 0, 0);
      week.delivered_at = at.toISOString();
      const ev = (h, status, detail) => {
        const t = new Date(d);
        t.setHours(h, 5, 0, 0);
        return { time: t.toISOString(), status, detail };
      };
      week.order.tracking_events = [
        ev(17, "delivered", "delivered"),
        ev(9, "out_for_delivery", "out_for_delivery"),
        ev(3, "in_transit", "received_at_origin_facility"),
      ];
      if (idx === 9) {
        week.order.delivery_photo_urls = [img("pod")];
        week.order.delivery_signed_by = "Front porch";
      }
    }
    if (isToday) {
      week.status = "IN_TRANSIT";
      week.order.tracking_status = "out_for_delivery";
      week.order.tracking_status_detail = "out_for_delivery";
      const t = new Date(today);
      t.setHours(8, 12, 0, 0);
      const t2 = new Date(today);
      t2.setDate(t2.getDate() - 1);
      t2.setHours(22, 40, 0, 0);
      week.order.tracking_events = [
        { time: t.toISOString(), status: "out_for_delivery", detail: "out_for_delivery" },
        { time: t2.toISOString(), status: "in_transit", detail: "received_at_origin_facility" },
      ];
    }
    // Scenario shaping for the upcoming weeks.
    if (weekIdx === 0 && editable) {
      week.meals_preselected = true;
      week.auto_picked = true;
      week.needs_selection = true;
      week.benefits = [{ label: "$10 off premium meals", status: "available", voucher_code: "PREMIUM10", one_time: true, expires_at: new Date(d.getTime() + 3 * 86400000).toISOString() }];
    }
    if (weekIdx === 1) {
      week.market_items[0].is_selected = true;
      week.market_items[0].selected_quantity = 1;
    }
    if (weekIdx === 2) {
      week.is_skipped = true;
      week.status = "SKIPPED";
      week.meals_selected = 0;
      for (const r of week.recipes) r.is_selected = false;
      week.allowed_actions = { mealSwap: false };
      week.order = null;
    }
    if (weekIdx === 3) {
      for (const r of week.recipes) r.is_selected = false;
      week.meals_selected = 0;
      week.needs_selection = editable;
      week.holiday_message = "Holiday week: your box arrives a day later than usual.";
      week.holiday_delivery_date = iso(new Date(d.getTime() + 86400000));
    }
    weeks.push(week);
  });

  const nextEditable = weeks.find((w) => w.is_editable && !w.is_skipped);
  const skipped = weeks.find((w) => w.is_skipped);
  const account = {
    selected_plan_total_price: 84.93,
    selected_plan_total_price_currency: "USD",
    menu_grace_weeks: grace,
    refresh_interval_minutes: 180,
    delivery_watch_interval_minutes: 15,
    delivery_in_progress: scenario === "delivery",
    next_delivery_price_breakdown: { sub_total: 95.9, shipping_amount: 10.99, discount_amount: 10, tax_amount: 0 },
    next_delivery_total_currency: "USD",
    next_box_discount: nextEditable ? { label: "$10 off premium meals", week_id: nextEditable.week_id, status: "available" } : null,
    next_payment_date: nextEditable ? iso(new Date(new Date(nextEditable.selection_deadline).getTime())) : null,
    next_box_coupon: null,
  };
  const summary = {
    subscription_status: "active",
    account_id: "31415926",
    selected_plan: "Classic Box",
    plan_preference: "quick",
    selected_plan_total_price: 84.93,
    selected_plan_total_price_currency: "USD",
    account_credit: 12.5,
    account_credit_currency: "USD",
    number_of_people: 2,
    required_meal_count: 3,
    boxes_received: 128,
    delivery_address: "123 Example Street, Springfield, IL 62701",
    upcoming_delivery_count: 4,
    weeks_needing_selection: weeks.filter((w) => w.needs_selection).length,
    weeks_needing_selection_ids: weeks.filter((w) => w.needs_selection).map((w) => w.week_id),
    skipped_week_count: 1,
    next_skipped_week: skipped ? skipped.week_id : null,
    next_skipped_week_id: skipped ? skipped.week_id : null,
    next_box_coupon: null,
    next_payment_date: account.next_payment_date,
    next_holiday_message: null,
    next_holiday_delivery_date: null,
    refresh_interval_minutes: 180,
    delivery_watch_interval_minutes: 15,
    delivery_in_progress: false,
    selected_plan_price_breakdown: { shipping_amount: 10.99, discount_amount: 0 },
    payment_method_expiring: scenario !== "clean",
    payment_method_expired: false,
    payment_card_brand: "visa",
    payment_card_last4: "4242",
    payment_card_expiry: iso(new Date(today.getFullYear(), today.getMonth() + 1, 1)).slice(0, 7),
    next_box_discount: account.next_box_discount,
  };

  const spending = (() => {
    const months = [];
    for (let i = 0; i < 14; i += 1) {
      const d = new Date(today.getFullYear(), today.getMonth() - i, 1);
      const boxes = i === 5 ? 1 : 3 + (i % 3);
      months.push({ month: `${d.getFullYear()}-${pad(d.getMonth() + 1)}`, amount: boxes * (86 + ((i * 13) % 25)), currency: "USD", box_count: boxes, upcoming: i === 0 });
    }
    const wk = [];
    for (let i = 0; i < 10; i += 1) {
      const d = new Date(nextMon);
      d.setDate(d.getDate() - (i - 1) * 7);
      wk.push({ delivery_date: iso(d), amount: 84.93 + ((i * 9) % 22), currency: "USD", upcoming: i === 0, discount: i === 3 ? 10 : 0, coupon_code: i === 3 ? "PREMIUM10" : null });
    }
    return { weeks: wk, months, total: { amount: 11141.88, box_count: 103, currency: "USD", discount: 45 } };
  })();

  const collections = [
    "Chicken Recipes", "Hall of Fame", "Carb Smart Recipes", "Calorie Smart Recipes", "Vegetarian Recipes", "Mediterranean Recipes",
    "American Recipes", "Bowl Recipes", "Pasta Recipes", "Breakfast & Brunch Recipes", "Flatbread Recipes", "Asian Food Recipes",
    "One Pot / One Pan Meals", "Pescatarian Recipes", "Burger Recipes", "Taco Recipes", "Kid-Friendly Recipes", "Noodle Recipes",
    "Plant-Based Recipes", "Risotto Recipes", "Korean Recipes", "Thai Recipes", "Chinese Recipes", "Italian Food Recipes",
  ].map((name) => ({ name, slug: name.toLowerCase().replace(/[^a-z0-9]+/g, "-"), path: name.toLowerCase().replace(/[^a-z0-9]+/g, "-") }));
  const catalog = DISHES.slice(0, 30).map(([name, headline], i) => ({
    recipe_id: rid(5000 + i),
    name,
    headline,
    image_url: img(`c${i}`),
    rating: 4.2 + ((i * 7) % 8) / 10,
    ratings_count: 150 + ((i * 1733) % 9000),
    prep_time_minutes: 20 + ((i * 5) % 30),
    url: `https://www.hellofresh.com/recipes/${name.toLowerCase().replace(/[^a-z0-9]+/g, "-")}`,
    is_favorite: i % 5 === 1,
  }));

  const profile = {
    options: {
      taste: {
        exclusions: ["beef", "pork", "fish", "shellfish", "nuts", "mushrooms", "olives", "brussel-sprouts", "egg"],
        dietaryPreferences: ["mostly-meat", "flexitarian", "pescatarian", "vegetarian"],
        cuisines: ["caribbean", "chinese", "classic-american", "french", "greek", "indian", "italian", "japanese", "korean", "mexican", "middle-eastern", "thai"],
        flavors: ["cheesy", "herbs", "spicy", "sweet", "smoky", "tangy"],
        dishTypes: ["bake", "bowl", "burger", "main-plus-sides", "pizza", "salad", "sandwich", "soups-stews", "stir-fry", "wrap", "noodle"],
        primaryProteins: ["beef", "chicken", "fish", "plant-based-proteins", "pork", "shrimp-prawns"],
        nutritions: ["high-protein", "low-calorie", "low-carb", "glp1-support", "plant-based"],
        mealTypes: ["quick-easy", "batch", "chef-style", "family-style"],
      },
      household: { adults: [1, 2, 3, 4, 5, 6], children: [0, 1, 2, 3, 4] },
      goals: { goals: ["try-new-recipes", "save-time", "eat-healthier", "save-money", "make-cooking-easy"] },
      meta: {
        fieldsWithNone: ["taste.exclusions"],
        primaryProteinsGroups: { pescatarian: ["salmon", "cod", "tuna", "shrimp-prawns"], vegetarian: ["tofu", "halloumi", "legumes", "meat-alternative"] },
      },
    },
    profile: {
      taste: {
        exclusions: [],
        dietaryPreferences: ["mostly-meat"],
        cuisines: { caribbean: 100, chinese: 100, "classic-american": 100, french: 100, italian: 100, thai: -100 },
        flavors: { cheesy: 100, herbs: 100, spicy: 100, sweet: 100 },
        dishTypes: { bake: 100, bowl: 100, burger: 100, "main-plus-sides": 100 },
        primaryProteins: { beef: 100, chicken: 100, fish: 100, "plant-based-proteins": 100, pork: 100, "shrimp-prawns": 100 },
        nutritions: ["high-protein", "low-calorie"],
        mealTypes: ["quick-easy", "batch", "family-style", "chef-style"],
      },
      household: { adults: 2, children: 0 },
      goals: { goals: ["try-new-recipes", "save-time"] },
    },
    completion: { total: 9, completed: 7, percent: 78, incomplete_fields: ["taste.flavors", "goals.goals"] },
  };

  const prepItems = (weekId) => [
    ["Salt", ""], ["Black Pepper", "3 teaspoon (tsp)"], ["Olive Oil", "4 tablespoon (tbsp)"], ["Butter", "3 tablespoon (tbsp)"],
    ["Eggs", "2 unit"], ["Sugar", "1 teaspoon (tsp)"], ["Vegetable Oil", "2 teaspoon (tsp)"], ["Soy Sauce", "1 tablespoon (tbsp) + 1 teaspoon (tsp)"],
    ["Garlic Powder", "½ teaspoon (tsp)"],
  ].map(([n, a], i) => ({ uid: `${weekId}:${n.toLowerCase()}`, summary: a ? `${n} — ${a}` : n, status: i === 1 ? "completed" : "needs_action", due: null }));

  return { weeks, account, summary, spending, collections, catalog, profile, prepItems };
}

// ---- fake hass ----------------------------------------------------------------------------

// Home Assistant's frontend/get_translations for this integration: the language's translation
// file over English, flattened to "component.hellofresh.<category>.<path>" keys, as it answers.
async function translations(origin, language, category) {
  const load = async (lang) => {
    const res = await fetch(`${origin}/translations/${lang}.json`);
    return res.ok ? res.json() : {};
  };
  const flat = (node, prefix, out) => {
    for (const [key, value] of Object.entries(node || {})) {
      if (value && typeof value === "object") flat(value, `${prefix}.${key}`, out);
      else out[`${prefix}.${key}`] = value;
    }
    return out;
  };
  const prefix = `component.hellofresh.${category}`;
  const english = flat((await load("en"))[category], prefix, {});
  const own = language === "en" ? {} : flat((await load(language))[category], prefix, {});
  return { resources: { ...english, ...own } };
}

export function buildHass(origin, { dark = false, scenario = "default", latency = 250, fail = "", language = "en" } = {}) {
  const fx = buildFixtures(origin, { scenario });
  const calls = [];
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const upcoming = fx.weeks.filter((w) => new Date(w.delivery_date) >= new Date(new Date().toDateString()) && !w.is_skipped && w.recipes.length);
  const prepWeeks = upcoming.slice(0, 2);
  const todo = {
    "todo.hellofresh_us_prep_list": fx.prepItems(prepWeeks[0] ? prepWeeks[0].week_id : "x"),
    "todo.hellofresh_us_prep_list_week_2": fx.prepItems(prepWeeks[1] ? prepWeeks[1].week_id : "y").slice(0, 4),
  };
  const states = {};
  const setState = (id, state, attributes = {}) => {
    states[id] = { entity_id: id, state: String(state), attributes, last_updated: new Date().toISOString() };
  };
  const refreshTodoStates = () => {
    setState("todo.hellofresh_us_prep_list", todo["todo.hellofresh_us_prep_list"].filter((i) => i.status !== "completed").length, { week_id: prepWeeks[0] && prepWeeks[0].week_id });
    setState("todo.hellofresh_us_prep_list_week_2", todo["todo.hellofresh_us_prep_list_week_2"].filter((i) => i.status !== "completed").length, { week_id: prepWeeks[1] && prepWeeks[1].week_id });
  };
  refreshTodoStates();
  setState("select.hellofresh_us_box_size", "3 meals × 2 servings", { options: ["2 meals × 2 servings", "3 meals × 2 servings", "4 meals × 2 servings", "5 meals × 2 servings", "3 meals × 4 servings"] });
  setState("select.hellofresh_us_delivery_day", "Monday 8AM - 8PM", { options: ["Monday 8AM - 8PM", "Tuesday 8AM - 8PM", "Wednesday 8AM - 8PM"] });
  setState("sensor.hellofresh_us_access_token_minutes_remaining", 42);
  setState("sensor.hellofresh_us_refresh_token_days_remaining", 26);
  setState("binary_sensor.hellofresh_us_write_actions_available", "on");
  setState("binary_sensor.hellofresh_us_payload_shape_changed", "off");
  const reg = (entity_id, translation_key) => [entity_id, { entity_id, platform: "hellofresh", translation_key, device_id: "dev1" }];
  const entities = Object.fromEntries([
    reg("todo.hellofresh_us_prep_list", "prep_list"),
    reg("todo.hellofresh_us_prep_list_week_2", "prep_list_week_2"),
    reg("select.hellofresh_us_box_size", "box_size"),
    reg("select.hellofresh_us_delivery_day", "delivery_day"),
    reg("sensor.hellofresh_us_access_token_minutes_remaining", "access_token_minutes_remaining"),
    reg("sensor.hellofresh_us_refresh_token_days_remaining", "refresh_token_days_remaining"),
    reg("binary_sensor.hellofresh_us_write_actions_available", "write_actions_available"),
    reg("binary_sensor.hellofresh_us_payload_shape_changed", "payload_shape_changed"),
  ]);
  const devices = { dev1: { id: "dev1", config_entries: ["entry1"], name: "HelloFresh (US)" } };

  // ?scenario=live: a box on the road in the Netherlands, where HelloFresh drives its own vans and
  // the integration turns the live tracker behind hftrack.nl into sensors. The next box's order
  // carries the tracked link, which is what ties the tracker to it.
  // (Local YYYY-MM-DD strings: `new Date("2026-10-05")` is UTC midnight, the day before west of UTC.)
  const liveWeek = fx.weeks
    .filter((w) => w.delivery_date >= iso(new Date()) && !w.is_skipped)
    .sort((a, b) => a.delivery_date.localeCompare(b.delivery_date))[0];
  if (scenario === "live" && liveWeek) {
    const url = "https://www.hftrack.nl/track/7Hq2xLm9Pz";
    Object.assign(liveWeek.order, { carrier: "HelloFresh", tracking_number: null, tracking_url: url, tracking_status: null, tracking_events: [] });
    const now = Date.now();
    setState("sensor.hellofresh_nl_delivery_tracking_phase", "On the way", {
      phase: "ON_THE_WAY",
      active: true,
      tracking_link_present: true,
      personal_customer_message: "Hi! This is Sanne. Your HelloFresh box is in my van and on its way to you.",
      delivery_time: "18:00 – 22:00",
      driver_name: "Sanne",
      amount_of_stops_before: 3,
      driver_location: { latitude: 52.3702, longitude: 4.8952 },
      customer_location: { latitude: 52.3584, longitude: 4.9187 },
      tracking_url: url,
      last_fetched: new Date(now - 2 * 60000).toISOString(),
    });
    setState("sensor.hellofresh_nl_delivery_tracking_eta", new Date(now + 38 * 60000).toISOString());
    Object.assign(
      entities,
      Object.fromEntries([
        reg("sensor.hellofresh_nl_delivery_tracking_phase", "delivery_tracking_phase"),
        reg("sensor.hellofresh_nl_delivery_tracking_eta", "delivery_tracking_eta"),
      ])
    );
  }

  const week = (id) => fx.weeks.find((w) => w.week_id === id);
  const hellofresh = {
    async get_weeks() {
      return { weeks: JSON.parse(JSON.stringify(fx.weeks)), account: fx.account };
    },
    async get_account_summary() {
      return fx.summary;
    },
    async get_menu_courses({ week_id, filters }) {
      const w = week(week_id);
      if (filters["main-protein"]) {
        const names = { beef: "Beef", poultry: "Poultry", pork: "Pork", "fish-seafood": "Seafood", vegetarian: "Veggie" };
        const wanted = new Set(filters["main-protein"].map((slug) => names[slug]));
        const ids = w.recipes.filter((r) => wanted.has(r.server_protein || r.preference)).map((r) => r.recipe_id);
        return { week_id, recipe_ids: ids, count: ids.length, filters };
      }
      const n = Object.values(filters).flat().length;
      const ids = w.recipes.filter((r, i) => (i + n) % 3 !== 0).map((r) => r.recipe_id);
      return { week_id, recipe_ids: ids, count: ids.length, filters };
    },
    async preview_meal_price({ week_id, recipe_ids, quantities }) {
      const w = week(week_id);
      let sub = 0;
      for (const id of recipe_ids) {
        const r = w.recipes.find((x) => x.recipe_id === id);
        const q = (quantities && quantities[id]) || 1;
        sub += (r ? r.price : 11.99) * 2 * q;
      }
      return { meal_count: recipe_ids.length, grand_total: Number((sub + 10.99 - 10).toFixed(2)), sub_total: Number(sub.toFixed(2)), shipping_amount: 10.99, tax_amount: 0, discount_amount: 10, coupon_code: "PREMIUM10", surcharges: [] };
    },
    async select_meals({ week_id, recipe_ids, quantities, market_quantities }) {
      const w = week(week_id);
      for (const r of w.recipes) {
        const on = recipe_ids.includes(r.recipe_id);
        r.is_selected = on;
        r.selected_quantity = on ? (quantities && quantities[r.recipe_id]) || 1 : null;
      }
      // A variant sharing a recipe id with its base must not double-select.
      w.meals_selected = recipe_ids.length;
      w.auto_picked = false;
      w.meals_preselected = false;
      w.needs_selection = false;
      if (market_quantities) applyMarket(w, market_quantities);
      return { downgraded: false };
    },
    async select_market_items({ week_id, quantities }) {
      applyMarket(week(week_id), quantities);
      return { downgraded: false };
    },
    async skip_week({ week_id }) {
      const w = week(week_id);
      w.is_skipped = true;
      w.status = "SKIPPED";
    },
    async unskip_week({ week_id }) {
      const w = week(week_id);
      w.is_skipped = false;
      w.status = "RUNNING";
      w.allowed_actions = { mealSwap: true };
    },
    async reschedule_week({ week_id, delivery_option }) {
      const w = week(week_id);
      const o = w.available_one_off_options.find((x) => x.handle === delivery_option);
      if (o) w.delivery_date = o.delivery_date;
    },
    async get_delivery_options() {
      return { delivery_options: [
        { handle: "mon-8-20", delivery_name: "Monday 8AM - 8PM", price: 0 },
        { handle: "tue-8-20", delivery_name: "Tuesday 8AM - 8PM", price: 0 },
        { handle: "wed-8-20", delivery_name: "Wednesday 8AM - 8PM", price: 2.99 },
      ] };
    },
    async get_presets() {
      return { presets: [
        { handle: "chefs-choice", name: "Chef's Choice", description: "Our most popular recipes, picked by our chefs." },
        { handle: "quick", name: "Quick & Easy", description: "Ready in 30 minutes or less." },
        { handle: "veggie", name: "Veggie", description: "Meat-free meals packed with flavour." },
        { handle: "family", name: "Family Friendly", description: "Kid-approved recipes the whole table will love." },
      ] };
    },
    async get_spending() {
      return fx.spending;
    },
    async get_food_profile() {
      return JSON.parse(JSON.stringify(fx.profile));
    },
    async set_food_profile(changes) {
      fx.profile.profile = { ...fx.profile.profile, ...changes };
      return { profile: fx.profile.profile };
    },
    async get_recipe_collections() {
      return { collections: fx.collections };
    },
    async get_catalog_recipes({ collection, search }) {
      if (search) return { recipes: fx.catalog.filter((r) => r.name.toLowerCase().includes(search.toLowerCase())), subcollections: [] };
      const offset = collection ? hash(collection) % 10 : 0;
      return {
        collection: collection || null,
        recipes: fx.catalog.slice(offset).concat(fx.catalog.slice(0, offset)),
        subcollections: collection === "noodle-recipes" ? [{ name: "Ramen", slug: "ramen", path: "noodle-recipes/ramen" }, { name: "Udon", slug: "udon", path: "noodle-recipes/udon" }] : [],
      };
    },
    async get_favorites() {
      return { favorites: fx.catalog.filter((r) => r.is_favorite) };
    },
    async add_favorite({ recipe_id }) {
      const r = fx.catalog.find((x) => x.recipe_id === recipe_id);
      if (r) r.is_favorite = true;
      return { favorite: r };
    },
    async remove_favorite({ recipe_id }) {
      const r = fx.catalog.find((x) => x.recipe_id === recipe_id);
      if (r) r.is_favorite = false;
      return { removed: true };
    },
    async get_recipe_detail({ recipe_id, servings }) {
      const all = [...fx.weeks.flatMap((w) => w.recipes), ...fx.catalog];
      const r = all.find((x) => x.recipe_id === recipe_id) || fx.catalog[0];
      const s = servings || 2;
      return { recipe: {
        recipe_id, name: r.name, headline: r.description || r.headline, image_url: r.image_url, prep_time_minutes: r.prep_time_minutes || 30,
        calories_kcal: r.calories_kcal || 650, difficulty: 1, rating: 4.4, servings: s, available_yields: [2, 4],
        labels: ["Protein Smart"], description: "A cosy weeknight favourite with bright, fresh flavours.", allergens: ["Milk", "Wheat"],
        ingredients: [
          { name: "Chicken Cutlets", amount: 10 * (s / 2), unit: "ounce", shipped: true, allergens: [] },
          { name: "Potatoes", amount: 12 * (s / 2), unit: "ounce", shipped: true, allergens: [] },
          { name: "Sour Cream", amount: 1.5 * (s / 2), unit: "tablespoon", shipped: true, allergens: ["Milk"] },
          { name: "Butter", amount: 1, unit: "tablespoon", shipped: false, allergens: ["Milk"] },
          { name: "Salt", shipped: false, allergens: [] },
        ],
        utensils: ["Baking sheet", "Large pan", "Medium pot"],
        steps: [
          { instructions: "Adjust rack to middle position and preheat oven to 425 degrees. Wash and dry produce.", timers: [] },
          { instructions: "Dice potatoes into ½-inch pieces. Boil until tender, 10-15 minutes.", timers: [{ name: "Boil", seconds: 720 }] },
          { instructions: "Pat chicken dry; season all over. Sear until golden, 2 minutes per side.", timers: [] },
        ],
        nutrition: { Calories: "780 kcal", Fat: "34 g", Carbohydrate: "60 g", Protein: "53 g" },
        url: "https://www.hellofresh.com/recipes/example", card_url: "https://www.hellofresh.com/recipecards/example.pdf",
      } };
    },
    async refresh_data() {},
  };
  function applyMarket(w, quantities) {
    for (const item of w.market_items) {
      const q = Number(quantities[item.item_id] || 0);
      item.selected_quantity = q;
      item.is_selected = q > 0;
    }
  }

  const hass = {
    themes: { darkMode: dark },
    states,
    entities,
    devices,
    language,
    // ?lang= gives the profile a language (and Home Assistant's other locale settings).
    ...(language !== "en" ? { locale: { language, number_format: "language", time_format: "language", first_weekday: "language" } } : {}),
    async callWS(msg) {
      calls.push({ ws: msg.type, data: { ...msg } });
      await sleep(Math.min(latency, 80));
      if (msg.type === "frontend/get_translations") return translations(origin, msg.language, msg.category);
      throw new Error(`Unknown command ${msg.type}`);
    },
    async callService(domain, service, data = {}, target = undefined, _notify = false, returnResponse = false) {
      calls.push({ domain, service, data: JSON.parse(JSON.stringify(data || {})), target });
      await sleep(latency);
      if (domain === "hellofresh") {
        if (fail && fail.split(",").includes(service)) throw new Error("HelloFresh is down");
        const fn = hellofresh[service];
        if (!fn) throw new Error(`Unknown service hellofresh.${service}`);
        const response = await fn(data || {});
        return returnResponse ? { response } : {};
      }
      if (domain === "todo" && service === "get_items") {
        const id = target.entity_id;
        return { response: { [id]: { items: JSON.parse(JSON.stringify(todo[id] || [])) } } };
      }
      if (domain === "todo" && service === "update_item") {
        const item = (todo[target.entity_id] || []).find((i) => i.uid === data.item);
        if (item) item.status = data.status;
        refreshTodoStates();
        return {};
      }
      if (domain === "select" && service === "select_option") {
        const s = states[target.entity_id];
        setState(target.entity_id, data.option, s.attributes);
        return {};
      }
      throw new Error(`Unknown service ${domain}.${service}`);
    },
  };
  return { hass, calls, fx };
}
