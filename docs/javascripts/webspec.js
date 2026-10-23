// Home page: the security-level dial.
//
// Each line (or inline piece) of the example request carries data-from and, optionally,
// data-to: the levels at which the harness shim adds it. Without this script the page shows
// the level-4 request and every level's facts.
(() => {
  const dial = document.querySelector(".ws-dial");
  if (!dial) return;

  const tabs = [...dial.querySelectorAll(".ws-level")];
  const pieces = [...dial.querySelectorAll("[data-from]")];
  const facts = [...dial.querySelectorAll("[data-level-fact]")];
  let touched = false;

  function show(level, animate = true) {
    for (const tab of tabs) {
      const on = Number(tab.dataset.level) === level;
      tab.setAttribute("aria-selected", String(on));
      tab.tabIndex = on ? 0 : -1;
    }
    for (const piece of pieces) {
      const on = level >= Number(piece.dataset.from) && level <= Number(piece.dataset.to ?? 4);
      const appearing = on && piece.hidden;
      piece.hidden = !on;
      piece.classList.remove("ws-new");
      if (animate && appearing) {
        void piece.offsetWidth; // restart the animation
        piece.classList.add("ws-new");
      }
    }
    for (const fact of facts) {
      fact.classList.toggle("is-on", Number(fact.dataset.levelFact) === level);
    }
  }

  for (const tab of tabs) {
    tab.addEventListener("click", () => {
      touched = true;
      show(Number(tab.dataset.level));
    });
  }

  dial.querySelector(".ws-levels").addEventListener("keydown", (event) => {
    const current = tabs.findIndex((tab) => tab.getAttribute("aria-selected") === "true");
    const next = { ArrowDown: current + 1, ArrowRight: current + 1, ArrowUp: current - 1,
                   ArrowLeft: current - 1, Home: 0, End: tabs.length - 1 }[event.key];
    if (next === undefined) return;
    event.preventDefault();
    touched = true;
    const tab = tabs[(next + tabs.length) % tabs.length];
    tab.focus();
    show(Number(tab.dataset.level));
  });

  dial.classList.add("ws-dial--live");
  show(0, false);

  // Hold the panel at its tallest level's height, so turning the dial never moves the page.
  // Measure again once the web fonts have loaded, and whenever the panel's width changes.
  const panel = dial.querySelector(".ws-dial__panel");
  let measuredWidth = 0;
  function reserve(force = false) {
    if (!force && panel.offsetWidth === measuredWidth) return;
    measuredWidth = panel.offsetWidth;
    const current = tabs.findIndex((tab) => tab.getAttribute("aria-selected") === "true");
    panel.style.minHeight = "";
    let tallest = 0;
    for (const tab of tabs) {
      show(Number(tab.dataset.level), false);
      tallest = Math.max(tallest, panel.offsetHeight);
    }
    panel.style.minHeight = `${tallest}px`;
    show(Math.max(current, 0), false);
  }
  reserve(true);
  document.fonts?.ready.then(() => reserve(true));
  addEventListener("resize", () => reserve());

  // Turn the dial once, from 0 to 4, the first time it is in view.
  if (matchMedia("(prefers-reduced-motion: reduce)").matches || !("IntersectionObserver" in window)) return;
  const observer = new IntersectionObserver(([entry]) => {
    if (!entry.isIntersecting) return;
    observer.disconnect();
    let level = 0;
    const timer = setInterval(() => {
      if (touched || level >= 4) return clearInterval(timer);
      show(++level);
    }, 1500);
  }, { threshold: 0.5 });
  observer.observe(dial);
})();

// Home page: the request specimen's method tabs.
//
// Each tab shows one of the walkthrough's level-0 exchanges, all answered 200 OK: a read
// (GET), a send (POST, with its definer) and a delete. Without this script the page shows
// the delete only.
(() => {
  const specimen = document.querySelector(".ws-specimen[data-methods]");
  if (!specimen) return;

  const tablist = specimen.querySelector(".ws-methods");
  const tabs = [...tablist.querySelectorAll("[role=tab]")];
  const requests = [...specimen.querySelectorAll(":scope > pre")];
  const only = [...specimen.querySelectorAll("[data-only]")];
  let touched = false;

  function show(method, animate = true) {
    for (const tab of tabs) {
      const on = tab.dataset.method === method;
      tab.setAttribute("aria-selected", String(on));
      tab.tabIndex = on ? 0 : -1;
    }
    for (const request of requests) {
      const on = request.dataset.method === method;
      const appearing = on && !request.classList.contains("is-on");
      request.classList.toggle("is-on", on);
      request.classList.remove("ws-in");
      if (animate && appearing) {
        void request.offsetWidth; // restart the animation
        request.classList.add("ws-in");
      }
    }
    for (const item of only) item.hidden = item.dataset.only !== method;
  }

  for (const tab of tabs) {
    tab.addEventListener("click", () => {
      touched = true;
      show(tab.dataset.method);
    });
  }

  tablist.addEventListener("keydown", (event) => {
    const current = tabs.findIndex((tab) => tab.getAttribute("aria-selected") === "true");
    const next = { ArrowRight: current + 1, ArrowLeft: current - 1, Home: 0, End: tabs.length - 1 }[event.key];
    if (next === undefined) return;
    event.preventDefault();
    touched = true;
    const tab = tabs[(next + tabs.length) % tabs.length];
    tab.focus();
    show(tab.dataset.method);
  });

  for (const request of requests) request.hidden = false;
  tablist.hidden = false;
  specimen.classList.add("ws-specimen--live");
  show("delete", false);

  // Hold the card at its tallest height, so switching never resizes it. The requests share one
  // grid cell, so only the legend changes: the send's has one more entry. Measure it with that
  // entry shown, then restore; again once the web fonts have loaded, and on resize.
  let measuredWidth = 0;
  function reserve(force = false) {
    if (!force && specimen.offsetWidth === measuredWidth) return;
    measuredWidth = specimen.offsetWidth;
    const shown = only.map((item) => item.hidden);
    specimen.style.minHeight = "";
    for (const item of only) item.hidden = false;
    specimen.style.minHeight = `${specimen.offsetHeight}px`;
    only.forEach((item, i) => { item.hidden = shown[i]; });
  }
  reserve(true);
  document.fonts?.ready.then(() => reserve(true));
  addEventListener("resize", () => reserve());

  // Show each method once, ending on the delete, the first time the card is in view.
  if (matchMedia("(prefers-reduced-motion: reduce)").matches || !("IntersectionObserver" in window)) return;
  const tour = ["get", "post", "delete"];
  const observer = new IntersectionObserver(([entry]) => {
    if (!entry.isIntersecting) return;
    observer.disconnect();
    let step = 0;
    const timer = setInterval(() => {
      if (touched || step >= tour.length) return clearInterval(timer);
      show(tour[step++]);
    }, 2200);
  }, { threshold: 0.5 });
  observer.observe(specimen);
})();
