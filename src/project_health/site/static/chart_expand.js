(function () {
  "use strict";

  // Generic "Expand chart" dialog (issue #156): one implementation for
  // every chart on every page -- a metric card, tone-over-time, the open
  // PR backlog's stacked charts, a peers line chart, the year-over-year
  // small multiples, the thread explorer's charts -- driven entirely by
  // the small, declarative `data-chart-meta` blob each chart's own
  // container carries (`site/chart_spec.py::chart_meta_json`). This file
  // never special-cases a chart by its page or its own field names beyond
  // what that blob declares (`timeField`/`timeType`/`seriesField`/
  // `hasBand`/`params`); a chart with no series field simply gets no
  // series checkboxes, one with no band layer gets no band toggle, and so
  // on, all through the same code path.

  // --- Shared spec registry (issue #156) ----------------------------------
  //
  // `static/app.js`'s `embedChart` and `static/thread_explorer.js`'s own
  // `embedChart` each register the *exact* resolved spec they just handed
  // `vegaEmbed` (post chart-window/yoy/tone-mix filtering, keyed by the
  // chart's `data-chart-id`) here, right before embedding it -- so this
  // dialog's own starting point for a chart is whatever that chart is
  // *currently* showing on the page, already filtered by that chart's own
  // existing on-page controls, rather than this file re-deriving (or
  // re-filtering) that from scratch. `requestEmbed` lets this file force a
  // chart inside a still-collapsed `<details class="page-section">` to
  // embed (and so register its spec) on demand -- e.g. a deep link
  // straight to a chart nobody has expanded yet.
  var registry = (window.__chartSpecRegistry = window.__chartSpecRegistry || {
    specs: {},
    set: function (id, spec) {
      if (id) {
        this.specs[id] = spec;
      }
    },
    get: function (id) {
      return id ? this.specs[id] : undefined;
    },
  });

  var dialog = document.getElementById("chart-expand-dialog");
  if (!dialog || typeof dialog.showModal !== "function") {
    // No dialog on this page, or a browser old enough to lack <dialog> --
    // the Expand buttons still render (hidden only by the no-JS rule) but
    // do nothing, same "progressive, never polyfilled" stance the rest of
    // this site takes for <details>/<summary>.
    return;
  }

  var titleEl = document.getElementById("chart-expand-title");
  var controlsEl = dialog.querySelector("[data-chart-expand-controls]");
  var mainEl = dialog.querySelector("[data-chart-expand-main]");
  var overviewEl = dialog.querySelector("[data-chart-expand-overview]");
  var closeBtn = dialog.querySelector("[data-chart-expand-close]");

  var state = {
    sourceEl: null,
    meta: null,
    triggerBtn: null,
    mainView: null,
    overviewView: null,
    seriesValues: [],
    seriesHidden: {},
    bandOn: true,
    timeValues: [],
    from: null,
    to: null,
    paramCleanups: [],
  };

  // --- Small, generic helpers ----------------------------------------------

  function cloneSpec(spec) {
    return JSON.parse(JSON.stringify(spec));
  }

  // The object whose own `.layer` array is this spec's actual mark layers --
  // the top level for an un-faceted layered spec (the M0/governance trend
  // charts), `spec.spec` for a faceted small-multiples spec (year-over-year,
  // message patterns, the thread explorer's label charts).
  function layerContainer(spec) {
    if (spec.spec && Array.isArray(spec.spec.layer)) {
      return spec.spec;
    }
    return spec;
  }

  // The object that actually owns this chart's x/y/color `encoding` --
  // the top level for most specs, `spec.spec` for a faceted small-
  // multiples spec, or (`peers_page.py::_line_chart_spec`, the one chart
  // in this project with no top-level `encoding` at all) the first layer
  // that has one. Every x-axis/series/legend fixup below goes through
  // this instead of assuming `spec.encoding` directly, so it keeps
  // working regardless of which of these three shapes a given chart's
  // own spec-building function happens to use.
  function encodingOwner(spec) {
    if (spec.spec && spec.spec.encoding) {
      return spec.spec;
    }
    if (spec.encoding) {
      return spec;
    }
    if (Array.isArray(spec.layer)) {
      for (var i = 0; i < spec.layer.length; i++) {
        if (spec.layer[i].encoding) {
          return spec.layer[i];
        }
      }
    }
    return null;
  }

  // A confidence-interval band layer (issue #156's "CI band toggle when
  // the spec has a band/errorband layer"): an area mark with its own `y2`
  // encoding -- every CI band in this project's charts is built this way
  // (`conversation_patterns_page.py`'s `_yoy_group_spec`/`_small_multiples_
  // spec`, `thread_explorer_page.py`'s label charts don't have one, but
  // the detection is generic either way).
  function isBandLayer(layer) {
    return (
      !!layer &&
      !!layer.mark &&
      (layer.mark.type === "area" || layer.mark === "area") &&
      !!layer.encoding &&
      !!layer.encoding.y2
    );
  }

  function distinctSorted(values) {
    var seen = {};
    var out = [];
    values.forEach(function (v) {
      if (v == null) {
        return;
      }
      var key = String(v);
      if (!seen[key]) {
        seen[key] = true;
        out.push(key);
      }
    });
    out.sort();
    return out;
  }

  // `distinct` values (already deduped, any order), reordered to match
  // `order` (`meta.seriesOrder` -- the chart's own declared natural order,
  // orchestrator review of PR #157) wherever a value appears in both;
  // anything in `distinct` but not in `order` (a declared order that's
  // stale, or simply absent) sorts alphabetically after every declared
  // value, so a chart with no `seriesOrder` behaves exactly as before.
  function orderedValues(distinct, order) {
    if (!order || !order.length) {
      return distinct;
    }
    var distinctSet = {};
    distinct.forEach(function (v) {
      distinctSet[v] = true;
    });
    var out = order.filter(function (v) {
      return distinctSet[v];
    });
    var placed = {};
    out.forEach(function (v) {
      placed[v] = true;
    });
    var rest = distinct.filter(function (v) {
      return !placed[v];
    });
    return out.concat(rest);
  }

  // Temporal axis format by this project's own `timeType`s (issue #156
  // fixup, orchestrator review of PR #157: "temporal axis labels must
  // include the year"). Only applies to a `type: "temporal"` x encoding
  // (the month-granularity charts) -- the "quarter"/"year" `timeType`s are
  // plain ordinal strings ("2013Q1", "2020") that already carry the year
  // as literal text, so there's nothing to format.
  var TEMPORAL_AXIS_FORMAT = { month: "%b %Y" };

  // The x-encoding fixup `filteredSpec` applies to whichever chart's x
  // field is declared in `meta` (issue #156 fixup, orchestrator review of
  // PR #157): drop the server's own default scale domain/tick step (see
  // `filteredSpec`'s own comment for why), force a year-inclusive format
  // on a genuinely temporal field, and keep ordinal-axis labels
  // (month/quarter/year strings alike) from colliding once this chart's
  // full history is in view.
  function applyTimeAxis(xEncoding, meta) {
    var next = Object.assign({}, xEncoding);
    delete next.scale;
    var axis = Object.assign({}, next.axis);
    delete axis.tickCount;
    if (next.type === "temporal") {
      axis.format = TEMPORAL_AXIS_FORMAT[meta.timeType] || axis.format;
    }
    if (axis.labelOverlap === undefined) {
      axis.labelOverlap = "parity";
    }
    next.axis = axis;
    return next;
  }

  function rowsOf(spec) {
    return (spec.data && Array.isArray(spec.data.values)) ? spec.data.values : [];
  }

  // How many distinct time values make up "N years" of this chart's own
  // time granularity -- used by the 1y/3y/5y presets, generically, for
  // every `timeType` this project's charts use (issue #156).
  var UNITS_PER_YEAR = { month: 12, quarter: 4, year: 1 };

  function unitsForYears(timeType, years) {
    return (UNITS_PER_YEAR[timeType] || 12) * years;
  }

  // --- Opening / closing ----------------------------------------------------

  function ensureVisible(el) {
    var body = el.closest ? el.closest(".page-section-body") : null;
    var details = body && body.parentElement;
    if (details && details.tagName === "DETAILS" && !details.open) {
      details.open = true;
    }
    if (registry.requestEmbed) {
      registry.requestEmbed(el);
    }
  }

  function readMeta(el) {
    var raw = el.getAttribute("data-chart-meta");
    if (!raw) {
      return null;
    }
    try {
      return JSON.parse(raw);
    } catch (err) {
      return null;
    }
  }

  function rawSpecOf(el) {
    var raw = el.getAttribute("data-vega-spec") || el.getAttribute("data-threads-chart-spec");
    if (!raw) {
      return null;
    }
    try {
      return JSON.parse(raw);
    } catch (err) {
      return null;
    }
  }

  function baseSpecFor(el, chartId) {
    var registered = registry.get(chartId);
    return registered ? cloneSpec(registered) : rawSpecOf(el);
  }

  function findChartElById(chartId) {
    var selector = '[data-chart-id="' + chartId.replace(/"/g, '\\"') + '"]';
    return document.querySelector(selector);
  }

  function openForChart(el, triggerBtn) {
    var meta = readMeta(el);
    if (!meta) {
      return;
    }
    cleanupParamMirrors();
    ensureVisible(el);

    state.sourceEl = el;
    state.meta = meta;
    state.triggerBtn = triggerBtn || null;
    state.seriesHidden = {};
    state.bandOn = true;
    state.from = null;
    state.to = null;

    var base = baseSpecFor(el, meta.id);
    state.timeValues = meta.timeField ? distinctSorted(rowsOf(base || {}).map(function (r) {
      return r[meta.timeField];
    })) : [];
    state.seriesValues = meta.seriesField
      ? orderedValues(
          distinctSorted(rowsOf(base || {}).map(function (r) {
            return r[meta.seriesField];
          })),
          meta.seriesOrder
        )
      : [];

    titleEl.textContent = meta.title || "Chart";
    buildControls(meta);
    renderOverview();
    renderMain();

    if (!dialog.open) {
      dialog.showModal();
    }
    writeHash();
  }

  function closeDialog() {
    if (dialog.open) {
      dialog.close();
    }
  }

  dialog.addEventListener("close", function () {
    finalizeView("mainView");
    finalizeView("overviewView");
    cleanupParamMirrors();
    clearHashIfOwned();
    if (state.triggerBtn && typeof state.triggerBtn.focus === "function") {
      state.triggerBtn.focus();
    }
    state.sourceEl = null;
    state.meta = null;
  });

  closeBtn.addEventListener("click", closeDialog);

  function finalizeView(key) {
    if (state[key]) {
      try {
        state[key].finalize();
      } catch (err) {
        // Ignore -- the dialog is closing either way.
      }
      state[key] = null;
    }
  }

  // --- Expand buttons -------------------------------------------------------

  document.querySelectorAll("[data-chart-expand]").forEach(function (btn) {
    // Every Expand button lives in its own tight `.chart-shell` wrapper
    // together with exactly the one chart `<div>` it expands (never
    // shared with another chart, even when several charts sit in the
    // same section/card-grid/loop body) -- see `templates/_charts.html`'s
    // own docstring for why a shell wrapper, not a same-container lookup,
    // is what keeps this unambiguous.
    var shell = btn.closest(".chart-shell");
    var chartEl = shell ? shell.querySelector("[data-chart-id]") : null;
    if (!chartEl) {
      return;
    }
    btn.addEventListener("click", function () {
      openForChart(chartEl, btn);
    });
  });

  // --- Controls: date range, series, CI band, declared params --------------

  function buildControls(meta) {
    controlsEl.innerHTML = "";

    if (meta.timeField && state.timeValues.length) {
      controlsEl.appendChild(buildDateRangeControl(meta));
    }
    if (meta.seriesField && state.seriesValues.length) {
      controlsEl.appendChild(buildSeriesControl(meta));
    }
    if (meta.hasBand) {
      controlsEl.appendChild(buildBandControl());
    }
    (meta.params || []).forEach(function (param) {
      var mirror = buildParamMirror(param);
      if (mirror) {
        controlsEl.appendChild(mirror);
      }
    });
  }

  function buildDateRangeControl(meta) {
    var wrap = document.createElement("fieldset");
    wrap.className = "chart-expand-control chart-expand-control--range";
    var legend = document.createElement("legend");
    legend.textContent = "Date range";
    wrap.appendChild(legend);

    var fromSelect = document.createElement("select");
    fromSelect.setAttribute("data-chart-expand-range", "from");
    var toSelect = document.createElement("select");
    toSelect.setAttribute("data-chart-expand-range", "to");
    state.timeValues.forEach(function (v) {
      var fromOpt = document.createElement("option");
      fromOpt.value = v;
      fromOpt.textContent = v;
      fromSelect.appendChild(fromOpt);
      var toOpt = document.createElement("option");
      toOpt.value = v;
      toOpt.textContent = v;
      toSelect.appendChild(toOpt);
    });
    fromSelect.value = state.from || state.timeValues[0];
    toSelect.value = state.to || state.timeValues[state.timeValues.length - 1];
    state.from = fromSelect.value;
    state.to = toSelect.value;

    function applyRange() {
      state.from = fromSelect.value;
      state.to = toSelect.value;
      renderMain();
      writeHash();
    }
    fromSelect.addEventListener("change", applyRange);
    toSelect.addEventListener("change", applyRange);

    var fromLabel = document.createElement("label");
    fromLabel.textContent = "From ";
    fromLabel.appendChild(fromSelect);
    var toLabel = document.createElement("label");
    toLabel.textContent = "To ";
    toLabel.appendChild(toSelect);
    wrap.appendChild(fromLabel);
    wrap.appendChild(toLabel);

    var presets = document.createElement("span");
    presets.className = "chart-expand-presets";
    [["1y", 1], ["3y", 3], ["5y", 5], ["All", null]].forEach(function (preset) {
      var label = preset[0];
      var years = preset[1];
      var btn = document.createElement("button");
      btn.type = "button";
      btn.textContent = label;
      btn.addEventListener("click", function () {
        if (years === null) {
          fromSelect.value = state.timeValues[0];
        } else {
          var units = unitsForYears(meta.timeType, years);
          var startIdx = Math.max(0, state.timeValues.length - units);
          fromSelect.value = state.timeValues[startIdx];
        }
        toSelect.value = state.timeValues[state.timeValues.length - 1];
        applyRange();
      });
      presets.appendChild(btn);
    });
    wrap.appendChild(presets);
    return wrap;
  }

  function buildSeriesControl(meta) {
    var wrap = document.createElement("fieldset");
    wrap.className = "chart-expand-control chart-expand-control--series";
    var legend = document.createElement("legend");
    legend.textContent = "Series";
    wrap.appendChild(legend);
    state.seriesValues.forEach(function (value) {
      var label = document.createElement("label");
      label.className = "chart-expand-series-item";
      var input = document.createElement("input");
      input.type = "checkbox";
      input.checked = !state.seriesHidden[value];
      input.addEventListener("change", function () {
        state.seriesHidden[value] = !input.checked;
        // The overview strip shows "the same series" as the main chart
        // (orchestrator review of PR #157) -- rebuild it too, not just
        // the main view.
        renderOverview();
        renderMain();
        writeHash();
      });
      label.appendChild(input);
      label.appendChild(document.createTextNode(" " + value));
      wrap.appendChild(label);
    });
    return wrap;
  }

  function buildBandControl() {
    var wrap = document.createElement("fieldset");
    wrap.className = "chart-expand-control chart-expand-control--band";
    var label = document.createElement("label");
    var input = document.createElement("input");
    input.type = "checkbox";
    input.checked = state.bandOn;
    input.addEventListener("change", function () {
      state.bandOn = input.checked;
      renderOverview();
      renderMain();
      writeHash();
    });
    label.appendChild(input);
    label.appendChild(document.createTextNode(" Show confidence interval band"));
    wrap.appendChild(label);
    return wrap;
  }

  // A chart-specific control the page itself already renders and already
  // knows how to apply (year-over-year's venue/from-year/to-year/cutoff,
  // tone-over-time's mode/cutoff, the thread explorer's own filters) --
  // this mirrors that *same* live `<select>`/`<input>` inside the dialog
  // rather than re-implementing its filtering math here: a change in the
  // dialog writes straight through to the real control and re-dispatches
  // its `change` event, so the page's own existing listener
  // (`applyYoyFilter`/`applyToneFilter`/the thread table's own filter)
  // does the actual work, then this dialog re-reads the chart's freshly
  // re-registered spec (issue #156: "reuse the existing on-page control
  // logic, don't duplicate math").
  function buildParamMirror(param) {
    var real = document.querySelector(param.selector);
    if (!real || (real.tagName !== "SELECT" && real.tagName !== "INPUT")) {
      return null;
    }
    var wrap = document.createElement("label");
    wrap.className = "chart-expand-control chart-expand-param";
    wrap.appendChild(document.createTextNode(param.name + " "));
    var mirror = real.cloneNode(true);
    mirror.removeAttribute("data-yoy-control");
    mirror.removeAttribute("data-tone-control");
    mirror.removeAttribute("data-threads-filter");
    mirror.value = real.value;

    function onMirrorChange() {
      real.value = mirror.value;
      real.dispatchEvent(new Event("change"));
      // The real control's own listener just re-embedded the page's
      // chart (and re-registered its spec, `app.js`'s `embedChart`) with
      // whatever this param changed (venue, cutoff, mode, ...) -- rebuild
      // the overview from that fresh spec too, not just the main view.
      renderOverview();
      renderMain();
      writeHash();
    }
    mirror.addEventListener("change", onMirrorChange);

    function onRealChange() {
      mirror.value = real.value;
    }
    real.addEventListener("change", onRealChange);
    state.paramCleanups.push(function () {
      real.removeEventListener("change", onRealChange);
    });

    wrap.appendChild(mirror);
    return wrap;
  }

  function cleanupParamMirrors() {
    state.paramCleanups.forEach(function (fn) {
      fn();
    });
    state.paramCleanups = [];
  }

  // --- Filtering + rendering --------------------------------------------------

  function filteredSpec() {
    var meta = state.meta;
    var spec = baseSpecFor(state.sourceEl, meta.id) || cloneSpec(rawSpecOf(state.sourceEl) || {});
    if (!spec.data) {
      return spec;
    }
    var values = rowsOf(spec);

    if (meta.timeField && (state.from || state.to)) {
      values = values.filter(function (row) {
        var v = row[meta.timeField];
        if (v == null) {
          return false;
        }
        v = String(v);
        if (state.from && v < state.from) {
          return false;
        }
        if (state.to && v > state.to) {
          return false;
        }
        return true;
      });
    }
    if (meta.seriesField) {
      values = values.filter(function (row) {
        return !state.seriesHidden[row[meta.seriesField]];
      });
    }
    spec.data = Object.assign({}, spec.data, { values: values });

    // A date-range (or series) narrower than the chart's own default
    // encoding scale must not stay visually clipped to that default --
    // dropping the explicit scale domain/tick step here falls back to
    // Vega-Lite's own auto-fit over whatever `data.values` now holds,
    // same "never duplicate the server's own windowing math" reasoning
    // `params` mirroring above gives for chart-specific controls.
    if (meta.timeField) {
      var xOwner = encodingOwner(spec);
      if (xOwner && xOwner.encoding.x) {
        xOwner.encoding = Object.assign({}, xOwner.encoding, {
          x: applyTimeAxis(xOwner.encoding.x, meta),
        });
      }
    }

    if (meta.hasBand && !state.bandOn) {
      var container = layerContainer(spec);
      container.layer = (container.layer || []).filter(function (l) {
        return !isBandLayer(l);
      });
    }
    return spec;
  }

  // Small-multiples facet sizing, duplicated from `static/app.js`/`static/
  // thread_explorer.js`'s own copies for the same reason those two already
  // duplicate it from each other (no shared loader between these
  // independent, non-module `<script>`s) -- computes `facet.columns`/
  // `spec.width` from the dialog's own resolved pixel width.
  var FACET_PANEL_TOTAL_MIN = 210;
  var FACET_AXIS_OVERHEAD = 65;
  var FACET_GAP = 24;
  var FACET_MAX_COLUMNS = 3;
  var FACET_MIN_BODY_WIDTH = 90;

  function sizeSpec(spec, width, height) {
    if (spec.facet && spec.spec) {
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
        Math.min(maxColumns, Math.floor((width + FACET_GAP) / (FACET_PANEL_TOTAL_MIN + FACET_GAP)))
      );
      var panelWidth = Math.max(
        FACET_MIN_BODY_WIDTH,
        Math.floor((width - (columns - 1) * FACET_GAP) / columns) - FACET_AXIS_OVERHEAD
      );
      spec.columns = columns;
      spec.spec = Object.assign({}, spec.spec, { width: panelWidth, height: Math.max(spec.spec.height || 140, 160) });
      return spec;
    }
    spec.width = width;
    // Use whatever vertical room `.chart-expand-chart-group`'s flex
    // layout actually gave the main chart (orchestrator review of PR
    // #157: "main chart should use the freed height") -- most of that
    // when an overview strip sits under it, nearly all of it when the
    // strip was omitted (a faceted chart) -- rather than a fixed floor
    // that left a few hundred px of dead space on taller dialogs.
    var targetHeight = Math.max(height || 0, typeof spec.height === "number" ? spec.height : 0, 200);
    spec.height = targetHeight;
    // Vega-Lite's default autosize (`contains: "content"`) budgets
    // `width`/`height` for the plot body only -- axis labels/titles and
    // (especially) a bottom/right color legend render *outside* that,
    // which at the dialog's own larger size is room enough to overflow it
    // sideways (verified: a peers/tone-mix/pr-backlog chart's legend
    // pushed the dialog ~150px past its own right edge) **and**, just as
    // importantly, to push the x-axis's own tick labels/title below the
    // bottom of `.chart-expand-main`'s box entirely -- rendered, but
    // invisible, hidden behind the overview strip's own box right under
    // it (orchestrator review of PR #157's own screenshot: "the main
    // chart's x-axis ... is not visible"). `type: "fit"` (not just
    // "fit-x") budgets *both* dimensions for the whole rendered chart --
    // axis and legend chrome included -- shrinking the plot body as
    // needed so the x-axis always ends up inside the height this function
    // was actually given, the same "give it the real budget, don't just
    // hand it a number and hope" fix `generate.py::_vega_lite_spec`
    // already documents for width alone on the on-page charts.
    spec.autosize = { type: "fit", contains: "padding" };
    return spec;
  }

  function renderMain() {
    if (!window.vegaEmbed || !state.meta) {
      return;
    }
    var spec = filteredSpec();
    var width = mainEl.clientWidth || mainEl.getBoundingClientRect().width || 600;
    var height = mainEl.clientHeight || mainEl.getBoundingClientRect().height || 320;
    spec = sizeSpec(spec, width, height);
    finalizeView("mainView");
    window
      .vegaEmbed(mainEl, spec, { actions: false, renderer: "svg" })
      .then(function (result) {
        state.mainView = result;
      })
      .catch(function () {
        // A chart failing to render in the dialog must never leave the
        // rest of the page (or the dialog's own controls) unusable.
      });
  }

  // A small overview strip (issue #156: "a Vega-Lite interval brush on a
  // small overview strip below the main chart, kept in sync with the
  // selects") -- orchestrator review of PR #157: a *real* miniature of the
  // main chart (same mark/series, a genuine time axis with year-inclusive
  // labels), not a synthetic row-count-by-bucket bar chart, sitting
  // directly under the main chart with no gap (`.chart-expand-chart-group`,
  // `base.html`). Always built from the chart's *full*, unfiltered-by-
  // date-range history (only series-hide/band-toggle apply, same "same
  // series" as the main chart) so every part of the timeline stays
  // brushable regardless of the current From/To selection. Omitted
  // entirely for a small-multiples (faceted) spec -- there's no single
  // sensible mini panel to brush there -- so `renderMain`'s own flex
  // layout gives the main chart that freed height instead.
  function renderOverview() {
    finalizeView("overviewView");
    overviewEl.innerHTML = "";
    var meta = state.meta;
    var base = baseSpecFor(state.sourceEl, meta.id) || rawSpecOf(state.sourceEl);
    // Small-multiples (faceted) specs have no single sensible mini panel
    // to show here -- `encodingOwner` returns `null` for them unless they
    // also happen to carry a (meaningless, per-panel) top-level encoding,
    // so this also explicitly excludes `base.facet`.
    var baseOwner = base && !base.facet ? encodingOwner(base) : null;
    var baseX = baseOwner && baseOwner.encoding.x;
    if (!window.vegaEmbed || !meta.timeField || state.timeValues.length < 2 || !baseX) {
      overviewEl.hidden = true;
      return;
    }
    overviewEl.hidden = false;

    var spec = cloneSpec(base);
    if (meta.seriesField) {
      spec.data.values = rowsOf(spec).filter(function (row) {
        return !state.seriesHidden[row[meta.seriesField]];
      });
    }
    if (meta.hasBand && !state.bandOn) {
      var container = layerContainer(spec);
      container.layer = (container.layer || []).filter(function (l) {
        return !isBandLayer(l);
      });
    }

    var owner = encodingOwner(spec);
    var xEncoding = applyTimeAxis(owner.encoding.x, meta);
    xEncoding.axis = Object.assign({}, xEncoding.axis, {
      title: null,
      grid: false,
      labelFontSize: 9,
    });
    var ownerEncoding = Object.assign({}, owner.encoding, { x: xEncoding });
    if (ownerEncoding.y) {
      ownerEncoding.y = Object.assign({}, ownerEncoding.y, { axis: null, title: null });
    }
    // No legend down here -- the dialog's own Series checkboxes already
    // show it; a second one would just eat into this strip's 60px.
    if (ownerEncoding.color) {
      ownerEncoding.color = Object.assign({}, ownerEncoding.color, { legend: null });
    }
    owner.encoding = ownerEncoding;

    var width = overviewEl.clientWidth || overviewEl.getBoundingClientRect().width || 600;
    spec.width = width;
    spec.height = 60;
    // `type: "fit"` (both dimensions, not just width) for the same reason
    // `sizeSpec` uses it for the main chart -- this strip's own x-axis
    // labels need to end up *inside* the 60px height given, not pushed
    // below it (orchestrator review of PR #157).
    spec.autosize = { type: "fit", contains: "padding" };
    spec.params = (spec.params || []).concat([
      { name: "chartExpandBrush", select: { type: "interval", encodings: ["x"] } },
    ]);

    window
      .vegaEmbed(overviewEl, spec, { actions: false, renderer: "svg" })
      .then(function (result) {
        state.overviewView = result;
        result.view.addSignalListener("chartExpandBrush", function (_name, value) {
          applyBrushSelection(value);
        });
      })
      .catch(function () {
        // The brush is a convenience on top of the From/To selects, which
        // stay fully functional even if this small chart fails to embed.
        overviewEl.hidden = true;
      });
  }

  // Reads whichever single key the interval selection's signal payload
  // carries (Vega-Lite names it after the resolved field -- which, for a
  // field with a `timeUnit` transform like the M0 charts' `window_end`,
  // isn't the plain field name but e.g. `yearmonth_window_end` -- so this
  // never assumes the key, just takes whatever's there) and maps it back
  // onto `state.timeValues`' own indices: a `[min, max]` pair of `Date`s/
  // epoch millis for a temporal x, or an array of the selected discrete
  // domain strings for an ordinal one (Vega-Lite's own interval-selection
  // semantics on a band/point scale) -- either way, every `state.
  // timeValues` entry that falls inside it becomes the new From/To.
  function applyBrushSelection(value) {
    var key = Object.keys(value || {})[0];
    var raw = key ? value[key] : null;
    if (!raw || !raw.length) {
      return;
    }
    var matched = [];
    if (raw[0] instanceof Date || typeof raw[0] === "number") {
      var lo = +raw[0];
      var hi = +raw[raw.length - 1];
      if (lo > hi) {
        var tmp = lo;
        lo = hi;
        hi = tmp;
      }
      state.timeValues.forEach(function (v, i) {
        var t = +new Date(v);
        if (t >= lo && t <= hi) {
          matched.push(i);
        }
      });
    } else {
      raw.forEach(function (v) {
        var idx = state.timeValues.indexOf(String(v));
        if (idx !== -1) {
          matched.push(idx);
        }
      });
    }
    if (!matched.length) {
      return;
    }
    var loIdx = Math.min.apply(null, matched);
    var hiIdx = Math.max.apply(null, matched);
    state.from = state.timeValues[loIdx];
    state.to = state.timeValues[hiIdx];
    var fromSelect = controlsEl.querySelector('[data-chart-expand-range="from"]');
    var toSelect = controlsEl.querySelector('[data-chart-expand-range="to"]');
    if (fromSelect) {
      fromSelect.value = state.from;
    }
    if (toSelect) {
      toSelect.value = state.to;
    }
    renderMain();
    writeHash();
  }

  // --- Downloads: PNG/SVG of the view, CSV of exactly the filtered rows ----

  function csvEscape(value) {
    var s = String(value == null ? "" : value);
    if (/[",\n]/.test(s)) {
      return '"' + s.replace(/"/g, '""') + '"';
    }
    return s;
  }

  function downloadBlob(filename, contents, mime) {
    var blob = new Blob([contents], { type: mime });
    var url = URL.createObjectURL(blob);
    var a = document.createElement("a");
    a.href = url;
    a.download = filename;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    URL.revokeObjectURL(url);
  }

  function downloadCsv() {
    var rows = rowsOf(filteredSpec());
    if (!rows.length) {
      return;
    }
    var columns = Object.keys(rows[0]);
    var lines = [columns.map(csvEscape).join(",")];
    rows.forEach(function (row) {
      lines.push(columns.map(function (c) {
        return csvEscape(row[c]);
      }).join(","));
    });
    var id = (state.meta && state.meta.id) || "chart";
    downloadBlob(id + "-filtered.csv", lines.join("\n"), "text/csv");
  }

  function downloadImage(kind) {
    if (!state.mainView || !state.mainView.view) {
      return;
    }
    var id = (state.meta && state.meta.id) || "chart";
    state.mainView.view
      .toImageURL(kind)
      .then(function (url) {
        var a = document.createElement("a");
        a.href = url;
        a.download = id + "." + kind;
        document.body.appendChild(a);
        a.click();
        document.body.removeChild(a);
      })
      .catch(function () {
        // A failed export must never break the dialog itself.
      });
  }

  dialog.querySelectorAll("[data-chart-expand-download]").forEach(function (btn) {
    btn.addEventListener("click", function () {
      var kind = btn.getAttribute("data-chart-expand-download");
      if (kind === "csv") {
        downloadCsv();
      } else {
        downloadImage(kind);
      }
    });
  });

  // --- Deep links (issue #156: "#chart=<id>&from=...&to=...&series=a,b&
  // cutoff=...") -------------------------------------------------------------

  function currentParamValues() {
    var out = {};
    (state.meta && state.meta.params ? state.meta.params : []).forEach(function (param) {
      var real = document.querySelector(param.selector);
      if (real) {
        out[param.name] = real.value;
      }
    });
    return out;
  }

  function writeHash() {
    if (!state.meta) {
      return;
    }
    var parts = ["chart=" + encodeURIComponent(state.meta.id)];
    if (state.from) {
      parts.push("from=" + encodeURIComponent(state.from));
    }
    if (state.to) {
      parts.push("to=" + encodeURIComponent(state.to));
    }
    if (state.meta.seriesField) {
      var shown = state.seriesValues.filter(function (v) {
        return !state.seriesHidden[v];
      });
      parts.push("series=" + encodeURIComponent(shown.join(",")));
    }
    if (state.meta.hasBand) {
      parts.push("band=" + (state.bandOn ? "1" : "0"));
    }
    var params = currentParamValues();
    Object.keys(params).forEach(function (name) {
      parts.push(encodeURIComponent(name) + "=" + encodeURIComponent(params[name]));
    });
    var newHash = "#" + parts.join("&");
    if (window.location.hash !== newHash) {
      window.history.replaceState(null, "", window.location.pathname + window.location.search + newHash);
    }
  }

  function clearHashIfOwned() {
    var hash = window.location.hash;
    if (hash && hash.indexOf("#chart=") === 0) {
      window.history.replaceState(null, "", window.location.pathname + window.location.search);
    }
  }

  function parseHash() {
    var hash = window.location.hash;
    if (!hash || hash.indexOf("#chart=") !== 0) {
      return null;
    }
    var out = {};
    hash
      .slice(1)
      .split("&")
      .forEach(function (pair) {
        var eq = pair.indexOf("=");
        if (eq === -1) {
          return;
        }
        var key = decodeURIComponent(pair.slice(0, eq));
        var value = decodeURIComponent(pair.slice(eq + 1));
        out[key] = value;
      });
    return out;
  }

  function applyHashState(hashState) {
    var chartId = hashState.chart;
    if (!chartId) {
      return;
    }
    var el = findChartElById(chartId);
    if (!el) {
      return;
    }
    openForChart(el, null);
    if (hashState.from) {
      state.from = hashState.from;
    }
    if (hashState.to) {
      state.to = hashState.to;
    }
    if (hashState.series != null) {
      var shown = hashState.series ? hashState.series.split(",") : [];
      state.seriesValues.forEach(function (v) {
        state.seriesHidden[v] = shown.indexOf(v) === -1;
      });
    }
    if (hashState.band != null) {
      state.bandOn = hashState.band !== "0";
    }
    (state.meta.params || []).forEach(function (param) {
      if (hashState[param.name] == null) {
        return;
      }
      var real = document.querySelector(param.selector);
      if (real) {
        real.value = hashState[param.name];
        real.dispatchEvent(new Event("change"));
      }
    });
    buildControls(state.meta);
    renderOverview();
    renderMain();
  }

  function applyHashOnLoad() {
    var hashState = parseHash();
    if (hashState) {
      applyHashState(hashState);
    }
  }

  window.addEventListener("hashchange", function () {
    // Only act on a hash that targets a chart this dialog isn't already
    // showing -- the dialog's own controls write the hash themselves
    // (`writeHash`) on every change, which would otherwise re-trigger
    // this listener in a loop.
    var hashState = parseHash();
    if (hashState && (!state.meta || hashState.chart !== state.meta.id)) {
      applyHashState(hashState);
    }
  });

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", applyHashOnLoad);
  } else {
    applyHashOnLoad();
  }
})();
