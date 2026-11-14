/*
 * HelloFresh card — Week
 * ----------------------
 * When each meal in a box gets eaten. The tab shows the seven days a box covers (from its
 * delivery day), each with its planned dinner — and breakfast and lunch for weeks with Market
 * items for them — and a tray of what isn't planned yet. A day's meal opens a sheet to pick a
 * box meal, a Market item bought that week, or a note ("Pizza night"), and to change when it
 * starts. Dinner can add an appetizer and a dessert.
 *
 * The plan lives in the integration (get_meal_plan / set_planned_meal / clear_planned_meal),
 * which also turns it into the Meals calendar and today's-meal sensors; nothing here writes to
 * HelloFresh.
 */

const WEEK_VERSION = new URL(import.meta.url).searchParams.get("v") || "unknown";
const stamp = encodeURIComponent(WEEK_VERSION);
const [L, UI] = await Promise.all([
  import(new URL(`./hellofresh-card-logic.js?v=${stamp}`, import.meta.url).href),
  import(new URL(`./hellofresh-card-ui.js?v=${stamp}`, import.meta.url).href),
]);

const { esc, t, ht } = L;
const { icon, pill } = UI;

// The plan is Home Assistant's: its days are that calendar's and its times that clock's, so a
// browser in another time zone shows the same "Wednesday, 6:00 PM" the kitchen tablet does.
const time = (iso) => L.fmtHaTime(iso);
const shortDay = (iso) => L.fmtDate(iso, { weekday: "short", month: "short", day: "numeric" });

export class WeekView {
  constructor(card) {
    this.card = card;
    this.weekId = null;
    this._stripWeek = null;
    // The plan sheet open now (its state object). A save closes the sheet it was made in, not
    // one opened while it was on its way.
    this._sheet = null;
  }

  // ---- data --------------------------------------------------------------------------------

  _weeks() {
    return L.planWeeks(this.card.weeks || []);
  }

  _week(weeks = this._weeks()) {
    let week = weeks.find((w) => w.week_id === this.weekId);
    if (!week) {
      week = L.defaultPlanWeek(weeks);
      this.weekId = week ? week.week_id : null;
    }
    return week;
  }

  onRefresh() {
    // The card re-reads the plan with every refresh; nothing of our own to fetch.
  }

  // How many of a week's box meals are planned: { planned, total } (servings of each meal).
  _progress(week, plan) {
    let planned = 0;
    let total = 0;
    for (const item of L.planItems(week)) {
      if (item.kind !== "meal") continue;
      total += item.quantity;
      planned += Math.min(item.quantity, L.itemUses(plan, item).count);
    }
    return { planned, total };
  }

  // ---- render ------------------------------------------------------------------------------

  render() {
    const card = this.card;
    const placeholder = card.weeksPlaceholder();
    if (placeholder) return placeholder;
    const plan = card.mealPlan;
    if (!plan) {
      if (card.mealPlanError) {
        return UI.loadError(t("weekplan.load_failed", { error: card.mealPlanError }), "refresh", { primary: true });
      }
      return `<div class="hf-skeletons" aria-label="${ht("common.loading_label")}"><div class="hf-skeleton"></div>
        <div class="hf-skeleton"></div></div>`;
    }
    const weeks = this._weeks();
    if (!weeks.length) {
      return `<div class="hf-empty">${icon("mdi:calendar-week")}${ht("weekplan.no_weeks")}</div>`;
    }
    const week = this._week(weeks);
    return `${this._strip(weeks, week, plan)}${this._head(week)}${this._notices(week)}
      ${this._tray(week, plan)}${this._days(week, plan)}`;
  }

  afterRender(root, context) {
    UI.syncStrip(root, this, this._week(), context);
  }

  // The Menu's week chips, counting what's planned rather than the box's state.
  _strip(weeks, selected, plan) {
    const today = L.haToday();
    const chips = weeks
      .map((w) => {
        const now = L.planDays(w).includes(today);
        const { planned, total } = this._progress(w, plan);
        const skipped = L.isSkipped(w);
        const dot = skipped || !total ? "" : planned >= total ? "ready" : "needs";
        const label = skipped
          ? t("state.skipped.short")
          : total
            ? t("weekplan.planned_count", { planned, total })
            : L.stateLabel(w);
        return `<button class="hf-weekchip${skipped ? " skipped" : ""}" data-action="select-week"
            data-week-id="${esc(w.week_id)}" aria-pressed="${w === selected}"
            title="${esc(`${w.display_name || w.week_id} · ${label}`)}">
            ${now ? `<span class="hf-wcnow">${ht("weekplan.this_week")}</span>` : ""}
            <span class="hf-wcday">${esc(L.fmtWeekday(w.delivery_date))}</span>
            <span class="hf-wcdate">${esc(L.fmtDateShort(w.delivery_date))}</span>
            <span class="hf-wcstate"><span class="hf-wcdot ${dot}"></span><span class="hf-wclabel">${esc(label)}</span></span>
          </button>`;
      })
      .join("");
    return `<div class="hf-weekbar">
        <button class="hf-iconbtn" data-action="strip-page" data-dir="-1"
          aria-label="${ht("week.earlier_weeks")}">${icon("mdi:chevron-left")}</button>
        <div class="hf-weekstrip" data-scroll-key="planstrip"><div class="hf-weektrack">${chips}</div></div>
        <button class="hf-iconbtn" data-action="strip-page" data-dir="1"
          aria-label="${ht("week.later_weeks")}">${icon("mdi:chevron-right")}</button>
      </div>`;
  }

  _head(week) {
    const card = this.card;
    const days = L.planDays(week);
    const range = t("weekplan.range", { start: shortDay(days[0]), end: shortDay(days[6]) });
    const hasMeals = L.planItems(week).some((i) => i.kind === "meal");
    const actions = [];
    if (card.hasView("menu") && hasMeals) {
      actions.push(`<button class="hf-btn sm" data-action="goto" data-view="menu" data-week-id="${esc(week.week_id)}">
        ${icon("mdi:silverware-fork-knife")}${ht("overview.view_meals")}</button>`);
    }
    return `<div class="hf-weekhead">
        <div class="hf-weektitle">
          <div class="hf-weekdate">${esc(range)}</div>
          <div class="hf-weekpills">${UI.statePill(week)}${week.display_name ? `<span class="hf-weeksub">${esc(week.display_name)}</span>` : ""}</div>
        </div>
        <div class="hf-actions">${actions.join("")}</div>
      </div>`;
  }

  _notices(week) {
    const card = this.card;
    const out = [];
    if (L.isSkipped(week)) {
      out.push(`<div class="hf-notice tone-muted">${icon("mdi:calendar-remove-outline")}
        <div class="hf-noticebody">${ht("weekplan.skipped")}</div></div>`);
    } else if (!L.planItems(week).some((i) => i.kind === "meal")) {
      const pick = L.isWeekEditable(week) && card.hasView("menu");
      out.push(`<div class="hf-notice tone-warn">${icon("mdi:silverware-fork-knife")}
        <div class="hf-noticebody">${ht("weekplan.pick_first")}</div>
        ${pick ? `<button class="hf-btn sm primary" data-action="goto" data-view="menu" data-week-id="${esc(week.week_id)}">${ht("overview.choose_meals")}</button>` : ""}</div>`);
    }
    return out.length ? `<div class="hf-stack">${out.join("")}</div>` : "";
  }

  _thumb(item, size = 120) {
    const src = item && item.kind !== "note" ? L.resizedImage(item.image_url, size) : "";
    if (src) return `<img class="hf-thumb" loading="lazy" src="${esc(src)}" alt="">`;
    const name = !item ? "mdi:plus" : item.kind === "note" ? "mdi:note-text-outline" : "mdi:silverware-variant";
    return `<span class="hf-plannoimg">${icon(name)}</span>`;
  }

  _itemSub(item) {
    const minutes = L.headlineMinutes(item);
    if (minutes) return L.formatMinutes(minutes);
    if (item.kind === "market" && item.group) return L.marketGroupLabel(item.group);
    return "";
  }

  // Not planned yet: what's left of the box (and breakfast or lunch bought), to drop on a day.
  _tray(week, plan) {
    const eligible = L.planItems(week).filter((i) => i.kind === "meal" || i.group === "breakfast" || i.group === "lunch");
    if (!eligible.length) return "";
    const items = L.trayItems(week, plan);
    const total = eligible.reduce((n, i) => n + i.quantity, 0);
    const left = items.reduce((n, i) => n + i.left, 0);
    const body = items.length
      ? `<div class="hf-tray">${items
          .map((item) => {
            const sub = this._itemSub(item);
            return `<button class="hf-trayitem" data-action="plan-item" data-kind="${esc(item.kind)}" data-id="${esc(item.id)}" data-focus-key="tray:${esc(item.kind)}:${esc(item.id)}">
                ${this._thumb(item)}
                <span class="hf-plantext"><span class="hf-planname">${esc(item.name)}</span>
                  ${sub ? `<span class="hf-plansub">${esc(sub)}</span>` : ""}</span>
                ${item.left > 1 ? `<span class="hf-traycount">×${item.left}</span>` : ""}
              </button>`;
          })
          .join("")}</div>`
      : `<div class="hf-notice tone-ok">${icon("mdi:check-circle-outline")}<div class="hf-noticebody">${ht("weekplan.all_planned")}</div></div>`;
    return `<div class="hf-sectionhead"><h2 class="hf-h2">${ht("weekplan.not_planned_yet")}</h2>
        ${items.length ? `<span class="hf-sectionnote">${ht("weekplan.tray_left", { count: left, total })}</span>` : ""}</div>
      ${body}`;
  }

  _days(week, plan) {
    const today = L.haToday();
    const shown = L.planMealsShown(week, plan);
    const labelled = shown.length > 1;
    const rows = L.planDays(week)
      .map((day) => {
        const lines = [];
        const adds = [];
        for (const meal of shown) {
          const slot = L.planSlot(plan, day, meal);
          if (slot || meal === "dinner") lines.push(this._line(day, meal, slot, labelled));
          else {
            adds.push(`<button class="hf-link quiet" data-action="plan-slot" data-day="${esc(day)}" data-meal="${esc(meal)}" data-focus-key="plan:${esc(day)}:${esc(meal)}">
              ${icon("mdi:plus")}${ht(`weekplan.meal.${meal}`)}</button>`);
          }
        }
        const date = L.parseLocalDate(day);
        const cls = ["hf-planday"];
        if (day === today) cls.push("today");
        else if (day < today) cls.push("past");
        return `<div class="${cls.join(" ")}"${day === today ? ' aria-current="date"' : ""}>
            <div class="hf-plandate"><span class="hf-pdow">${esc(L.fmtWeekday(day))}</span><span class="hf-pnum">${date.getDate()}</span></div>
            <div class="hf-planmeals">${lines.join("")}${adds.length ? `<div class="hf-planadds">${adds.join("")}</div>` : ""}</div>
          </div>`;
      })
      .join("");
    return `<div class="hf-sectionhead"><h2 class="hf-h2">${ht("weekplan.days")}</h2></div>
      <div class="hf-rows hf-plandays">${rows}</div>`;
  }

  // A day's meal. The line is a plain container (a click anywhere on it still opens the plan
  // sheet); its one "open" control is a real button — "Plan" on an empty line, the meal's name
  // on a planned one — and the photo that opens the recipe is that button's sibling.
  _line(day, meal, slot, labelled) {
    const card = this.card;
    const label = labelled ? `<span class="hf-planmeal">${ht(`weekplan.meal.${meal}`)}</span>` : "";
    const open = `data-action="plan-slot" data-day="${esc(day)}" data-meal="${esc(meal)}"`;
    const focusKey = `data-focus-key="plan:${esc(day)}:${esc(meal)}"`;
    const where = t("weekplan.sheet_title", { meal: t(`weekplan.meal.${meal}`), date: L.fmtLongDate(day) });
    if (!slot) {
      return `<div class="hf-planline empty" ${open}>
          ${this._thumb(null)}
          <span class="hf-plantext">${label}<span class="hf-planname">${ht("weekplan.not_planned")}</span></span>
          <span class="hf-planside"><button class="hf-btn sm" ${open} ${focusKey} aria-label="${esc(`${t("weekplan.plan")}: ${where}`)}">${ht("weekplan.plan")}</button></span>
        </div>`;
    }
    const lead = slot.main || slot.appetizer || slot.dessert;
    const sub = [];
    const minutes = slot.main ? L.headlineMinutes(slot.main) : null;
    if (minutes) sub.push(L.formatMinutes(minutes));
    if (slot.main) {
      for (const course of ["appetizer", "dessert"]) if (slot[course]) sub.push(`+ ${slot[course].name}`);
    }
    const recipeId = this._recipeId(lead);
    const thumb = recipeId
      ? `<button class="hf-planthumb" data-action="open-recipe" data-recipe-id="${esc(recipeId)}" aria-label="${ht("weekplan.view_recipe", { name: lead.name })}">${this._thumb(lead)}</button>`
      : this._thumb(lead);
    const gone = !L.stillInBox(slot.main, card.weeks) ? pill(t("weekplan.no_longer_in_box"), "warn", "mdi:alert-outline") : "";
    const when = slot.start && slot.end ? `${time(slot.start)}–${time(slot.end)}` : "";
    return `<div class="hf-planline" ${open}>
        ${thumb}
        <span class="hf-plantext">${label}<button class="hf-open hf-planname" ${open} ${focusKey} aria-label="${esc(`${where}: ${L.slotTitle(slot)}`)}">${esc(L.slotTitle(slot))}</button>
          ${sub.length ? `<span class="hf-plansub">${esc(sub.join(" · "))}</span>` : ""}</span>
        <span class="hf-planside">${gone}${when ? `<span class="hf-plantime">${esc(when)}</span>` : ""}${icon("mdi:chevron-right")}</span>
      </div>`;
  }

  // The recipe behind a planned meal or Market item (Market items keep it on the week's item).
  _recipeId(item) {
    if (!item || item.kind === "note") return null;
    if (item.kind === "meal") return item.id;
    const week = (this.card.weeks || []).find((w) => w.week_id === item.week_id);
    const match = week && (week.market_items || []).find((m) => m.item_id === item.id);
    return (match && match.recipe_id) || null;
  }

  // ---- events ------------------------------------------------------------------------------

  onClick(_ev, el) {
    if (!el) return;
    const card = this.card;
    const weeks = this._weeks();
    switch (el.getAttribute("data-action")) {
      case "select-week":
        this.weekId = el.getAttribute("data-week-id");
        card.renderView();
        break;
      case "strip-page":
        UI.pageStrip(el, Number(el.getAttribute("data-dir")) || 0);
        break;
      case "plan-slot":
        this._openSlot(el.getAttribute("data-day"), el.getAttribute("data-meal"));
        break;
      case "plan-item": {
        const item = L.planItems(this._week(weeks)).find(
          (i) => i.kind === el.getAttribute("data-kind") && i.id === el.getAttribute("data-id")
        );
        if (item) this._openItem(item);
        break;
      }
      case "open-recipe":
        card.openRecipe(el.getAttribute("data-recipe-id"));
        break;
      default:
        break;
    }
  }

  // ---- plan a meal (the sheet) -------------------------------------------------------------

  _courses(meal) {
    return meal === "dinner" ? L.PLAN_COURSES : ["main"];
  }

  _openSlot(day, meal) {
    const card = this.card;
    const week = this._week();
    if (!week || !day || !L.PLAN_MEALS.includes(meal)) return;
    const slot = L.planSlot(card.mealPlan, day, meal);
    const sel = {};
    for (const course of this._courses(meal)) {
      const item = slot && slot[course];
      sel[course] = item ? (item.kind === "note" ? { kind: "note", text: item.name } : { ...item }) : null;
    }
    const defaults = (card.mealPlan.meals || {})[meal] || {};
    const state = {
      day,
      meal,
      week,
      sel,
      planned: Boolean(slot),
      // What the note field holds, whether or not the note is the chosen main right now.
      note: sel.main && sel.main.kind === "note" ? sel.main.text : "",
      start: slot && slot.start ? L.hhmm(slot.start) : defaults.start || "",
      moreOpen: false,
    };
    const title = t("weekplan.sheet_title", { meal: t(`weekplan.meal.${meal}`), date: L.fmtLongDate(day) });
    this._sheet = state;
    card.openSheet({
      kind: "mealplan",
      narrow: true,
      label: title,
      render: () => this._slotSheet(state, title),
      onClick: (_ev, el) => this._onSlotClick(state, el),
      // Typing a note chooses it: on `input` as the text arrives, and again on `change` when
      // the field is left (the same handler; the second call finds nothing to do).
      onInput: (ev) => this._onNoteTyped(state, ev),
      onChange: (ev) => this._onNoteTyped(state, ev),
      onClose: () => this._sheetClosed(state),
    });
  }

  _sheetClosed(state) {
    if (this._sheet === state) this._sheet = null;
  }

  _slotSheet(state, title) {
    const card = this.card;
    const plan = card.mealPlan;
    const items = L.planItems(state.week);
    const courses = this._courses(state.meal);
    const sections = courses.map((course) => {
      const fits = items.filter((i) => L.itemFits(i, state.meal, course));
      const rows = fits.map((item) => this._option(state, course, item, plan));
      if (course !== "main") {
        if (!fits.length) return "";
        rows.unshift(this._noneOption(state, course));
      } else {
        rows.push(this._noteOption(state));
      }
      const heading =
        course === "main"
          ? state.meal === "dinner"
            ? t("weekplan.course.main")
            : ""
          : t("weekplan.optional", { course: t(`weekplan.course.${course}`) });
      return `<div class="hf-planopts">${heading ? `<h3 class="hf-h3">${esc(heading)}</h3>` : ""}${rows.join("")}</div>`;
    });
    // Everything else from the week, as a main: a box meal for lunch, a lunch kit for dinner.
    const others = items.filter((i) => !courses.some((course) => L.itemFits(i, state.meal, course)));
    const more = others.length
      ? `<details class="hf-planmore" ${state.moreOpen || others.some((i) => L.sameItem(state.sel.main, i)) ? "open" : ""}>
          <summary data-action="toggle-more">${ht("weekplan.more_from_week")}</summary>
          <div class="hf-planopts">${others.map((item) => this._option(state, "main", item, plan)).join("")}</div>
        </details>`
      : "";
    // While a save is on its way nothing in the sheet takes another tap.
    const busy = card.busy ? "disabled" : "";
    return `
      ${UI.sheetHead(title, state.week.display_name || "")}
      <div class="hf-sheetbody">
        ${sections.join("")}${more}
        <label class="hf-planfield"><span class="hf-h3">${ht("weekplan.start")}</span>
          <input class="hf-planinput" type="time" data-start-input data-focus-key="slot:start" value="${esc(state.start)}" ${busy}></label>
      </div>
      <div class="hf-sheetfoot">
        ${state.planned ? `<button class="hf-btn danger" data-action="clear-slot" data-focus-key="slot:clear" ${busy}>${ht("weekplan.clear")}</button>` : ""}
        <span class="hf-spacer"></span>
        <button class="hf-btn" data-close-sheet>${ht("common.cancel")}</button>
        <button class="hf-btn primary" data-action="save-slot" data-focus-key="slot:save" ${busy}>${ht("weekplan.save")}</button>
      </div>`;
  }

  _option(state, course, item, plan) {
    const on = L.sameItem(state.sel[course], item);
    const uses = L.itemUses(plan, item, { day: state.day, meal: state.meal, course });
    const left = item.quantity - uses.count;
    const disabled = (!on && left <= 0) || this.card.busy;
    const status =
      !on && left <= 0
        ? t("weekplan.planned_on", { days: uses.days.map((d) => L.fmtWeekday(d)).join(", ") })
        : item.quantity > 1 || uses.count
          ? t("weekplan.left", { count: Math.max(0, on ? left - 1 : left) })
          : "";
    const src = L.resizedImage(item.image_url, 120);
    const sub = this._itemSub(item);
    return `<button class="hf-optrow${on ? " on" : ""}" data-action="pick" data-course="${esc(course)}" data-kind="${esc(item.kind)}"
        data-id="${esc(item.id)}" data-focus-key="slot:${esc(course)}:${esc(item.kind)}:${esc(item.id)}" aria-pressed="${on}" ${disabled ? "disabled" : ""}>
        <span class="hf-radio" aria-hidden="true"></span>
        ${src ? `<img class="hf-optimg dish" loading="lazy" src="${esc(src)}" alt="">` : `<span class="hf-optimg">${icon("mdi:silverware-variant")}</span>`}
        <span class="hf-opttext"><span class="hf-optname">${esc(item.name)}</span>${sub ? `<span class="hf-optsub">${esc(sub)}</span>` : ""}</span>
        ${status ? `<span class="hf-optprice included">${esc(status)}</span>` : ""}
      </button>`;
  }

  _noneOption(state, course) {
    const on = !state.sel[course];
    return `<button class="hf-optrow${on ? " on" : ""}" data-action="pick-none" data-course="${esc(course)}" data-focus-key="slot:${esc(course)}:none"
        aria-pressed="${on}" ${this.card.busy ? "disabled" : ""}>
        <span class="hf-radio" aria-hidden="true"></span><span class="hf-opttext"><span class="hf-optname">${ht("weekplan.none")}</span></span>
      </button>`;
  }

  // "Something else": a note in place of a meal. Its radio is a real button, reachable with Tab
  // like every other option, and the text field sits beside it rather than inside a control. A
  // click anywhere on the row still chooses it (the row keeps the action for the mouse).
  _noteOption(state) {
    const sel = state.sel.main;
    const on = Boolean(sel && sel.kind === "note");
    const busy = this.card.busy ? "disabled" : "";
    return `<div class="hf-optrow hf-planote${on ? " on" : ""}" data-action="pick-note" data-course="main">
        <button class="hf-planotepick" data-action="pick-note" data-course="main" data-focus-key="slot:main:note" aria-pressed="${on}"
          aria-label="${ht("weekplan.something_else")}" ${busy}><span class="hf-radio" aria-hidden="true"></span><span class="hf-optimg">${icon("mdi:note-text-outline")}</span></button>
        <span class="hf-opttext"><span class="hf-optname">${ht("weekplan.something_else")}</span>
          <input class="hf-planinput" type="text" maxlength="80" data-note-input data-focus-key="slot:note" value="${esc(state.note)}"
            placeholder="${ht("weekplan.something_else_hint")}" aria-label="${ht("weekplan.something_else")}" ${busy}></span>
      </div>`;
  }

  // The sheet's typed values, kept across re-renders (each pick redraws the sheet).
  _capture(state) {
    const root = this.card.shadowRoot;
    if (!root) return;
    const note = root.querySelector("[data-note-input]");
    if (note) {
      state.note = note.value;
      if (state.sel.main && state.sel.main.kind === "note") state.sel.main.text = note.value;
    }
    const start = root.querySelector("[data-start-input]");
    if (start) state.start = start.value;
    const more = root.querySelector(".hf-planmore");
    if (more) state.moreOpen = more.open;
  }

  // Text arriving in the note field makes the note the chosen main: someone who tabs to the
  // field and types never clicks its radio. The field itself is left alone (it has the caret,
  // perhaps a half-composed character); only the options around it are brought in line.
  _onNoteTyped(state, ev) {
    const input = ev && ev.target && ev.target.closest ? ev.target.closest("[data-note-input]") : null;
    if (!input) return;
    state.note = input.value;
    if (state.sel.main && state.sel.main.kind === "note") {
      state.sel.main.text = input.value;
      return;
    }
    if (!input.value.trim()) return;
    state.sel.main = { kind: "note", text: input.value };
    const sheet = input.closest(".hf-sheet");
    if (!sheet) return;
    const items = L.planItems(state.week);
    for (const row of sheet.querySelectorAll('button.hf-optrow[data-action="pick"][data-course="main"]')) {
      const item = items.find((i) => i.kind === row.getAttribute("data-kind") && i.id === row.getAttribute("data-id"));
      if (item) UI.replaceNode(row, this._option(state, "main", item, this.card.mealPlan));
    }
    const noteRow = input.closest(".hf-planote");
    if (noteRow) {
      noteRow.classList.add("on");
      const pick = noteRow.querySelector(".hf-planotepick");
      if (pick) pick.setAttribute("aria-pressed", "true");
    }
  }

  _onSlotClick(state, el) {
    if (!el) return;
    const card = this.card;
    const action = el.getAttribute("data-action");
    if (action === "toggle-more") return; // <details> opens itself
    if (card.busy) return; // a save is on its way
    if (action === "save-slot") return this._saveSlot(state);
    if (action === "clear-slot") return this._clearSlot(state);
    const course = el.getAttribute("data-course");
    if (!course) return;
    this._capture(state);
    let focusKey = el.getAttribute("data-focus-key");
    if (action === "pick-note") {
      if (state.sel.main && state.sel.main.kind === "note") return; // already chosen: leave the field be
      state.sel.main = { kind: "note", text: state.note };
      focusKey = "slot:note"; // on to typing it
    } else if (action === "pick-none") state.sel[course] = null;
    else if (action === "pick") {
      const item = L.planItems(state.week).find(
        (i) => i.kind === el.getAttribute("data-kind") && i.id === el.getAttribute("data-id")
      );
      if (!item) return;
      // Picking the chosen main again leaves it chosen; an optional course toggles off.
      state.sel[course] = course !== "main" && L.sameItem(state.sel[course], item) ? null : item;
    }
    card.renderSheet();
    // Each pick redraws the sheet: the keyboard goes back to the row just picked, or into the
    // note field.
    if (focusKey) UI.focusKeyed(card.shadowRoot && card.shadowRoot.querySelector(".hf-sheet"), [focusKey]);
  }

  _payload(item) {
    if (item.kind === "note") return { text: item.text };
    if (item.kind === "market") return { market_item_id: item.id, week_id: item.week_id };
    return { recipe_id: item.id, week_id: item.week_id };
  }

  _same(want, have) {
    if (!want || !have) return !want && !have;
    if (want.kind === "note") return have.kind === "note" && have.name === want.text.trim();
    return L.sameItem(want, have);
  }

  // Send a save's calls one after another. Each is [service, data, label], the label naming the
  // part of the meal it writes, so that when one fails the user is told which parts were saved
  // and which were not (a dinner with a starter and a dessert is three calls).
  async _run(day, calls, done) {
    const card = this.card;
    // One save at a time: a second tap, or a tap on another day in the tray's picker, while the
    // first is on its way must not send its own.
    if (card.busy) return;
    const sheet = this._sheet;
    const mine = () => sheet !== null && this._sheet === sheet;
    if (!calls.length) {
      if (mine()) card.closeSheet();
      return;
    }
    card.setSaving(t("weekplan.saving"));
    card.renderSheet(); // its controls are disabled until the save is through
    let failed = null;
    let response = null;
    const saved = [];
    try {
      for (const [service, data, label] of calls) {
        response = await card.call(service, { date: day, ...data });
        saved.push(label);
      }
    } catch (err) {
      failed = (err && err.message) || String(err);
    }
    card.setSaving(null);
    if (response && response.days) card.setMealPlan(response);
    else await card.loadMealPlan();
    // The sheet the save was made in closes once it went through. A plan sheet still open (that
    // one after a failure, with what is left to save still chosen, or one opened meanwhile) was
    // drawn with its buttons off, and is drawn again to give them back.
    if (!failed && mine()) card.closeSheet();
    else if (card.sheetKind === "mealplan") card.renderSheet();
    if (!failed) {
      card.toast(done);
      return;
    }
    const names = (labels) => [...new Set(labels.filter(Boolean))].join(", ");
    const rest = calls.slice(saved.length).map(([, , label]) => label);
    card.toast(
      saved.length
        ? t("weekplan.save_partial", { saved: names(saved), failed: names(rest), error: failed })
        : t("weekplan.save_failed", { error: failed }),
      true
    );
  }

  _saveSlot(state) {
    this._capture(state);
    const existing = L.planSlot(this.card.mealPlan, state.day, state.meal) || {};
    const courses = this._courses(state.meal);
    const want = { ...state.sel };
    // A note typed with no main chosen at all is the main, even if no event announced the
    // typing (text dropped or pasted in by something that fires none).
    if (!want.main && state.note.trim()) want.main = { kind: "note", text: state.note };
    if (want.main && want.main.kind === "note" && !want.main.text.trim()) want.main = null;
    const meal = { meal: state.meal };
    const courseName = (course) => t(`weekplan.course.${course}`);
    if (courses.every((c) => !want[c])) {
      const clear = state.planned ? [["clear_planned_meal", meal, t(`weekplan.meal.${state.meal}`)]] : [];
      return this._run(state.day, clear, t("weekplan.cleared"));
    }
    const sets = [];
    const clears = [];
    for (const course of courses) {
      if (this._same(want[course], existing[course])) continue;
      if (want[course]) sets.push(["set_planned_meal", { ...meal, course, ...this._payload(want[course]) }, courseName(course)]);
      else clears.push(["clear_planned_meal", { ...meal, course }, courseName(course)]);
    }
    const current = existing.start ? L.hhmm(existing.start) : ((this.card.mealPlan.meals || {})[state.meal] || {}).start;
    if (state.start && state.start !== current) {
      if (sets.length) sets[sets.length - 1][1].start_time = state.start;
      else sets.push(["set_planned_meal", { ...meal, start_time: state.start }, t("weekplan.start_time")]);
    }
    return this._run(state.day, [...sets, ...clears], t("weekplan.saved"));
  }

  _clearSlot(state) {
    return this._run(state.day, [["clear_planned_meal", { meal: state.meal }, t(`weekplan.meal.${state.meal}`)]], t("weekplan.cleared"));
  }

  // ---- plan an item (from the tray): pick its day ------------------------------------------

  _openItem(item) {
    const card = this.card;
    const week = this._week();
    const { meal, course } = L.suggestedSlot(item);
    const title = t("weekplan.item_title", { name: item.name });
    const state = { item };
    this._sheet = state;
    card.openSheet({
      kind: "mealplan",
      narrow: true,
      label: title,
      onClose: () => this._sheetClosed(state),
      render: () => {
        const plan = card.mealPlan;
        const rows = L.planDays(week)
          .map((day) => {
            const slot = L.planSlot(plan, day, meal);
            const current = slot && slot[course] ? slot[course].name : "";
            return `<button class="hf-optrow" data-action="plan-day" data-day="${esc(day)}" data-focus-key="plan-day:${esc(day)}" ${card.busy ? "disabled" : ""}>
                <span class="hf-plandate"><span class="hf-pdow">${esc(L.fmtWeekday(day))}</span><span class="hf-pnum">${L.parseLocalDate(day).getDate()}</span></span>
                <span class="hf-opttext"><span class="hf-optname">${esc(L.fmtLongDate(day))}</span>
                  <span class="hf-optsub">${current ? ht("weekplan.replaces", { name: current }) : ht("weekplan.not_planned")}</span></span>
              </button>`;
          })
          .join("");
        return `
          ${UI.sheetHead(title, t(course === "main" ? `weekplan.meal.${meal}` : `weekplan.course.${course}`))}
          <div class="hf-sheetbody"><div class="hf-planopts">${rows}</div></div>`;
      },
      onClick: (_ev, el) => {
        if (!el || el.getAttribute("data-action") !== "plan-day") return;
        this._run(el.getAttribute("data-day"), [["set_planned_meal", { meal, course, ...this._payload(item) }, t(`weekplan.course.${course}`)]], t("weekplan.saved"));
      },
    });
  }
}
