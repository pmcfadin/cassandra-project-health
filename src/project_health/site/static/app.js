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

  // --- Small-multiples facet sizing (issue #133) -------------------------
  //
  // `conversation_patterns_page._yoy_group_spec` (and `thread_explorer.js`'s
  // own copy of this same function, for the thread-explorer label chart)
  // produce a Vega-Lite *faceted* spec -- one mini-chart per label -- with
  // no top-level `width` (a faceted spec's width lives on `spec.width`,
  // the per-panel view, not the top level `embedChart` used to override
  // for the old single-view bar chart). This computes `facet.columns` and
  // `spec.width` from the container's own resolved pixel width so panels
  // wrap to however many columns actually fit -- down to one on a narrow
  // phone -- instead of ever needing a horizontal scrollbar (reviewer
  // feedback on #120/#124: "labels graphs don't wrap (can't see those on
  // right)").
  //
  // `FACET_PANEL_TOTAL_MIN`/`FACET_AXIS_OVERHEAD`/`FACET_GAP` are
  // empirically measured, not guessed: each facet column in one of these
  // specs needs its own y-axis gutter (`resolve.scale.y: "independent"`
  // gives every panel its own scale/ticks, so Vega-Lite draws a full axis
  // per column, not just the leftmost one) plus Vega-Lite's own ~20px
  // inter-column facet spacing, on top of the plot body width
  // (`spec.width`) this function sets. Compiling real specs at this
  // panel's exact mark/encoding shape with `vl2vg`/measuring the
  // resulting SVG's rendered width (not guessed from the Vega-Lite docs)
  // at a sweep of container widths from 320px to 1920px is what these
  // numbers come from -- `FACET_AXIS_OVERHEAD`/`FACET_GAP` carry a margin
  // above the measured ~45-58px/~20px so a few extra characters in a
  // tick label never tips a column over the container edge.
  var FACET_PANEL_TOTAL_MIN = 210;
  var FACET_AXIS_OVERHEAD = 65;
  var FACET_GAP = 24;
  var FACET_MAX_COLUMNS = 3;
  var FACET_MIN_BODY_WIDTH = 90;

  function applyFacetColumns(spec, width) {
    if (!spec.facet || !spec.spec) {
      return spec;
    }
    var facetField = spec.facet.field;
    var labelCount = 1;
    if (facetField && spec.data && Array.isArray(spec.data.values)) {
      var seen = {};
      spec.data.values.forEach(function (row) {
        if (row && row[facetField] != null) {
          seen[row[facetField]] = true;
        }
      });
      labelCount = Math.max(1, Object.keys(seen).length);
    }
    var maxColumns = Math.max(1, Math.min(labelCount, FACET_MAX_COLUMNS));
    var columns = Math.max(
      1,
      Math.min(
        maxColumns,
        Math.floor((width + FACET_GAP) / (FACET_PANEL_TOTAL_MIN + FACET_GAP))
      )
    );
    var panelWidth = Math.max(
      FACET_MIN_BODY_WIDTH,
      Math.floor((width - (columns - 1) * FACET_GAP) / columns) - FACET_AXIS_OVERHEAD
    );
    var next = JSON.parse(JSON.stringify(spec));
    // `columns` is a top-level sibling of `facet`/`spec` in Vega-Lite's
    // standalone facet-operator form -- nested inside `facet` itself, the
    // compiler silently ignores it and lays out every panel in one row
    // (verified by compiling with `vl2vg`; see `_yoy_group_spec`'s own
    // docstring for the write-up).
    next.columns = columns;
    next.spec = Object.assign({}, next.spec, { width: panelWidth });
    return next;
  }

  // --- Year-over-year chart controls (issue #120) ------------------------
  //
  // `conversation_patterns_page._yoy_group_spec` ships each panel's *whole*
  // dataset (every venue/cutoff/year row that cleared the §5.1 floor) as
  // `data.values` -- there's nothing else to fetch, so filtering to the
  // controls' current venue/year-range/cutoff is just an array filter
  // re-applied on every embed, first paint included. That means the
  // controls' server-rendered `selected` options (from `_yoy_default_range`/
  // `_yoy_context`) and this filter never have to be kept in sync by hand:
  // this function is the only thing that ever narrows the data down.
  function currentYoyControls() {
    var form = document.querySelector("[data-yoy-controls]");
    if (!form) {
      return null;
    }
    var venueEl = form.querySelector('[data-yoy-control="venue"]');
    var fromEl = form.querySelector('[data-yoy-control="from-year"]');
    var toEl = form.querySelector('[data-yoy-control="to-year"]');
    var cutoffEl = form.querySelector('[data-yoy-control="cutoff"]');
    return {
      venue: venueEl ? venueEl.value : null,
      fromYear: fromEl ? Number(fromEl.value) : -Infinity,
      toYear: toEl ? Number(toEl.value) : Infinity,
      cutoff: cutoffEl ? cutoffEl.value : form.getAttribute("data-yoy-default-cutoff"),
    };
  }

  function applyYoyFilter(el, spec) {
    if (!el.hasAttribute("data-yoy-chart") || !spec.data || !spec.data.values) {
      return spec;
    }
    var controls = currentYoyControls();
    if (!controls) {
      return spec;
    }
    var next = Object.assign({}, spec);
    next.data = Object.assign({}, spec.data, {
      values: spec.data.values.filter(function (row) {
        var year = Number(row.year);
        return (
          row.venue === controls.venue &&
          row.cutoff === controls.cutoff &&
          year >= controls.fromYear &&
          year <= controls.toYear
        );
      }),
    });
    return next;
  }

  document.querySelectorAll("[data-yoy-control]").forEach(function (el) {
    el.addEventListener("change", renderAllCharts);
  });

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
    spec = applyYoyFilter(el, spec);

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
    var resolvedSpec = spec.facet
      ? applyFacetColumns(spec, width)
      : Object.assign({}, spec, { width: width });

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
