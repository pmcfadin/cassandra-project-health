(function () {
  "use strict";

  // Views currently embedded, keyed by their container element, so a
  // re-render (window resize, or the chart-window toggle below) finalizes
  // the old view before replacing it instead of stacking a second <svg>
  // inside the same container.
  var embeddedResults = new WeakMap();

  // --- Chart time-window toggle (issue #28) ---------------------------
  //
  // Every chart's spec (`generate.py`/`governance_page.py`,
  // `chart_spec.chart_window`) defaults its x-domain to the last 36
  // months and carries the full-history domain (plus, for the single-
  // series charts, both windows' tick spacing) in `spec.usermeta.
  // chartWindow` — inert JSON that Vega-Lite/vega-embed ignore, read back
  // out here so toggling never needs a second network request or a
  // server round-trip. The chosen window ("recent" | "full") is
  // remembered in localStorage so it survives a page reload; every
  // localStorage access is wrapped in try/catch since it can throw (a
  // private browsing window, blocked site data) and a broken preference
  // must never break the chart itself, just fall back to the default.
  var CHART_WINDOW_STORAGE_KEY = "cph-chart-window";

  function readStoredWindowPreference() {
    try {
      var stored = window.localStorage.getItem(CHART_WINDOW_STORAGE_KEY);
      return stored === "full" ? "full" : "recent";
    } catch (err) {
      return "recent";
    }
  }

  function writeStoredWindowPreference(value) {
    try {
      window.localStorage.setItem(CHART_WINDOW_STORAGE_KEY, value);
    } catch (err) {
      // Ignore -- the toggle still works for the rest of this page view
      // via `windowPreference` below; it just won't persist across reloads.
    }
  }

  var windowPreference = readStoredWindowPreference();

  function applyChartWindow(spec) {
    var chartWindow = spec && spec.usermeta && spec.usermeta.chartWindow;
    if (!chartWindow || !spec.encoding || !spec.encoding.x) {
      return spec;
    }
    var domain = chartWindow.domain && chartWindow.domain[windowPreference];
    if (!domain) {
      return spec;
    }
    // Clone rather than mutate `spec` in place: the caller may re-embed
    // the same parsed spec object again later (e.g. on the next resize or
    // toggle), and it must still carry the *original* domain each time.
    var next = JSON.parse(JSON.stringify(spec));
    next.encoding.x.scale = Object.assign({}, next.encoding.x.scale, { domain: domain });
    var step = chartWindow.tickStep && chartWindow.tickStep[windowPreference];
    if (step && next.encoding.x.axis && next.encoding.x.axis.tickCount) {
      next.encoding.x.axis.tickCount = Object.assign({}, next.encoding.x.axis.tickCount, {
        step: step,
      });
    }
    // The y-axis override (`chart_spec.recent_value_domain`): restricting
    // only the x-domain doesn't stop a low-n outlier month from still
    // flattening the y-scale once it's scrolled out of view, since
    // Vega-Lite auto-fits the y-scale to every value in `data.values`
    // regardless of the visible x-domain (issue #28). `yDomain.full` is
    // `null` by design -- switching to full history removes this override
    // so the chart shows its true, unflattened range.
    if (chartWindow.yDomain && next.encoding.y) {
      var yDomain = chartWindow.yDomain[windowPreference];
      var yScale = Object.assign({}, next.encoding.y.scale);
      if (yDomain) {
        yScale.domain = yDomain;
      } else {
        delete yScale.domain;
      }
      next.encoding.y.scale = yScale;
    }
    return next;
  }

  function updateToggleButtons() {
    var isFull = windowPreference === "full";
    document.querySelectorAll("[data-chart-window-toggle]").forEach(function (btn) {
      btn.setAttribute("aria-pressed", isFull ? "true" : "false");
      btn.textContent = isFull ? "Show recent" : "Full history";
    });
  }

  function setWindowPreference(value) {
    windowPreference = value === "full" ? "full" : "recent";
    writeStoredWindowPreference(windowPreference);
    updateToggleButtons();
    renderAllCharts();
  }

  document.querySelectorAll("[data-chart-window-toggle]").forEach(function (btn) {
    btn.addEventListener("click", function () {
      setWindowPreference(windowPreference === "full" ? "recent" : "full");
    });
  });
  updateToggleButtons();

  // --- Chart embedding --------------------------------------------------

  function embedChart(el) {
    if (!window.vegaEmbed) {
      return;
    }
    var raw = el.getAttribute("data-vega-spec");
    if (!raw) {
      return;
    }
    var spec;
    try {
      spec = JSON.parse(raw);
    } catch (err) {
      return;
    }
    spec = applyChartWindow(spec);

    var previous = embeddedResults.get(el);
    if (previous) {
      previous.finalize();
      embeddedResults.delete(el);
    }

    // The spec's own `width` is the string "container" (see generate.py),
    // which asks vega-embed to size the chart from its container via a
    // ResizeObserver. In practice that first measurement can race with
    // layout and resolve to 0 (observed: `svg.marks` rendered with
    // width="0" height="155" on first paint, with no console error) —
    // so instead of trusting "container" mode, resolve a concrete pixel
    // width from the container's own layout right now and pass that.
    var width = el.clientWidth || el.getBoundingClientRect().width || 300;
    var resolvedSpec = Object.assign({}, spec, { width: width });

    window
      .vegaEmbed(el, resolvedSpec, { actions: false, renderer: "svg" })
      .then(function (result) {
        embeddedResults.set(el, result);
      })
      .catch(function () {
        // A chart failing to render must never break the rest of the
        // page; the noscript fallback / data download links still work.
      });
  }

  function renderAllCharts() {
    document.querySelectorAll("[data-vega-spec]").forEach(embedChart);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", renderAllCharts);
  } else {
    renderAllCharts();
  }

  // Re-resolve each chart's width on viewport/layout changes (e.g. a
  // narrow mobile viewport, or rotating a device) rather than leaving it
  // pinned to whatever width happened to be available on first render.
  var resizeTimer = null;
  window.addEventListener("resize", function () {
    if (resizeTimer) {
      clearTimeout(resizeTimer);
    }
    resizeTimer = setTimeout(renderAllCharts, 150);
  });
})();
