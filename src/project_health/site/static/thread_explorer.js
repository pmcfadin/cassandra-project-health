(function () {
  "use strict";

  // Thread explorer table (issue #122, D27): fetches the published
  // `data/conversation-threads.json` rows file once, then filters, sorts,
  // paginates and exports entirely client-side -- same pattern as
  // `governance.js`'s commit-history table (issue #97), minus the
  // lazily-loaded "older history" split this page doesn't need. Every row
  // is a plain fact already computed server-side (`thread_explorer_page.
  // py`); this file never recomputes or re-labels a row as a verdict, and
  // never sorts by severity by default (D27: "no 'worst threads' view, no
  // pre-sorted-by-severity default, no highlight styling of negative
  // rows").

  var PAGE_SIZE = 50;

  var section = document.querySelector("[data-threads-table]");
  if (!section) {
    return;
  }

  var tbody = section.querySelector("[data-threads-tbody]");
  var statusEl = section.querySelector("[data-threads-status]");
  var pageLabel = section.querySelector("[data-threads-page-label]");
  var prevBtn = section.querySelector("[data-threads-prev]");
  var nextBtn = section.querySelector("[data-threads-next]");
  var exportButtons = Array.prototype.slice.call(
    section.querySelectorAll("[data-threads-export]")
  );
  var filterInputs = Array.prototype.slice.call(
    section.querySelectorAll("[data-threads-filter]")
  );
  var sortHeaders = Array.prototype.slice.call(section.querySelectorAll("[data-threads-sort]"));

  // --- Charts above the table (issue #124) ---------------------------------
  //
  // Both charts are driven by the *same* filter state as the table: every
  // call to `applyFiltersAndSort` below re-derives each chart's data from
  // `state.filtered` -- the identical row list the table itself renders --
  // using the JS ports of `thread_explorer_page.py`'s `year_outcome_rows`/
  // `label_year_share_rows` (also pytest-tested there against synthetic
  // rows, so the aggregation math has one canonical, tested definition).
  // Each chart <div>'s `data-threads-chart-spec` attribute carries the
  // *initial* (unfiltered) Vega-Lite spec Python built for it -- its
  // `mark`/`encoding`/color scale are kept as-is on every re-render; only
  // `data.values` is replaced, same "clone spec, swap data.values, re-embed"
  // pattern as `app.js`'s year-over-year chart filter.

  // Fixed order, mirroring `thread_explorer_page.CHART_OUTCOME_CATEGORIES`.
  var OUTCOME_CATEGORIES = [
    "none",
    "resolved",
    "escalated",
    "escalated, then de-escalated",
    "abandoned after friction",
  ];

  // Mirrors `conversation_patterns_page.CONSTRUCTIVE_LABELS`/`NEGATIVE_LABELS`
  // -- fixed display order, never alphabetical (same reasoning as #120).
  var CONSTRUCTIVE_LABELS = [
    "acknowledgment",
    "compromise_offer",
    "constructive_counterargument",
    "evidence_based_argument",
    "resolution_marker",
    "technical_disagreement",
  ];
  var NEGATIVE_LABELS = [
    "dismissiveness",
    "hostility",
    "personal_attack",
    "sarcasm",
    "gatekeeping",
    "status_authority_invocation",
  ];

  function humanizeLabel(label) {
    var s = String(label).replace(/_/g, " ");
    return s.charAt(0).toUpperCase() + s.slice(1);
  }

  // Mirrors `thread_explorer_page._chart_outcome_category`.
  function outcomeCategory(row) {
    var f = row.outcome_flags || {};
    if (f.escalated && f["de-escalated"]) {
      return "escalated, then de-escalated";
    }
    if (f.escalated) {
      return "escalated";
    }
    if (f.resolved) {
      return "resolved";
    }
    if (f["abandoned after friction"]) {
      return "abandoned after friction";
    }
    return "none";
  }

  // Mirrors `thread_explorer_page.year_outcome_rows`.
  function yearOutcomeRows(rows) {
    var counts = {};
    rows.forEach(function (row) {
      if (!row.year) {
        return;
      }
      var key = row.year + "\u0000" + outcomeCategory(row);
      counts[key] = (counts[key] || 0) + 1;
    });
    var out = Object.keys(counts).map(function (key) {
      var parts = key.split("\u0000");
      return { year: parts[0], outcome: parts[1], count: counts[key] };
    });
    out.sort(function (a, b) {
      if (a.year !== b.year) {
        return a.year < b.year ? -1 : 1;
      }
      return OUTCOME_CATEGORIES.indexOf(a.outcome) - OUTCOME_CATEGORIES.indexOf(b.outcome);
    });
    return out;
  }

  // Mirrors `thread_explorer_page.label_year_share_rows`.
  function labelYearShareRows(rows, labels) {
    var totals = {};
    rows.forEach(function (row) {
      if (!row.year) {
        return;
      }
      totals[row.year] = (totals[row.year] || 0) + 1;
    });
    var counts = {};
    rows.forEach(function (row) {
      if (!row.year || !row.label_counts) {
        return;
      }
      labels.forEach(function (label) {
        if (row.label_counts[label]) {
          var key = label + "\u0000" + row.year;
          counts[key] = (counts[key] || 0) + 1;
        }
      });
    });
    var years = Object.keys(totals).sort();
    var out = [];
    labels.forEach(function (label) {
      years.forEach(function (year) {
        var total = totals[year];
        var count = counts[label + "\u0000" + year] || 0;
        out.push({
          label: label,
          label_display: humanizeLabel(label),
          year: year,
          count: count,
          total: total,
          share: total ? count / total : 0,
        });
      });
    });
    return out;
  }

  // --- Small-multiples facet sizing (issue #133) ---------------------------
  //
  // `thread_explorer_page._label_year_group_spec` produces a faceted
  // Vega-Lite spec (one mini-chart per label) with no top-level `width` --
  // mirrors `app.js`'s own `applyFacetColumns` (same reasoning: compute
  // `facet.columns`/`spec.width` from the container's resolved pixel width
  // so panels wrap instead of ever needing a horizontal scrollbar). Kept as
  // its own copy here rather than imported -- this file and `app.js` are
  // two separate, non-module `<script>`s with no shared loader, same
  // reasoning the `CONSTRUCTIVE_LABELS`/`NEGATIVE_LABELS` constants above
  // are already duplicated rather than shared.
  //
  // `FACET_PANEL_TOTAL_MIN`/`FACET_AXIS_OVERHEAD`/`FACET_GAP` are
  // empirically measured (see `app.js`'s own copy of this function for
  // the full write-up): each facet column's own y-axis gutter
  // (`resolve.scale.y: "independent"`) plus Vega-Lite's own inter-column
  // facet spacing adds real width on top of `spec.width` alone, measured
  // by compiling real specs with `vl2vg` and checking the rendered SVG's
  // width at a sweep of container widths from 320px to 1920px.
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
    // `columns` is a top-level sibling of `facet`/`spec`, not nested
    // inside `facet` -- see `app.js`'s own copy of this function for why.
    next.columns = columns;
    next.spec = Object.assign({}, next.spec, { width: panelWidth });
    return next;
  }

  var CHART_KEYS = ["outcome", "label-constructive", "label-negative"];
  var chartEmbeds = {};
  var chartBaseSpecs = {};
  var chartShowShare = false; // outcome chart's counts <-> share-of-year toggle

  CHART_KEYS.forEach(function (key) {
    var el = document.querySelector('[data-threads-chart="' + key + '"]');
    if (!el) {
      return;
    }
    var raw = el.getAttribute("data-threads-chart-spec");
    if (!raw) {
      return;
    }
    try {
      chartBaseSpecs[key] = JSON.parse(raw);
    } catch (err) {
      // Leave unset -- `renderCharts` below skips any chart with no spec.
    }
  });

  function setFilterValue(name, value) {
    filterInputs.forEach(function (el) {
      if (el.getAttribute("data-threads-filter") === name) {
        el.value = value;
      }
    });
  }

  // Issue #124: "Clicking a bar applies that year (+ outcome or label) as
  // a table filter." The table's own Outcome filter matches a single
  // boolean flag (`rowMatches` above), not this chart's composite
  // "escalated, then de-escalated" category -- clicking that segment
  // applies the closest single-flag filter the table actually supports.
  function onChartClick(key, datum) {
    if (!datum || !datum.year) {
      return;
    }
    setFilterValue("year_from", datum.year);
    setFilterValue("year_to", datum.year);
    if (key === "outcome" && datum.outcome) {
      setFilterValue("outcome", datum.outcome.split(",")[0]);
    } else if (key !== "outcome" && datum.label) {
      setFilterValue("label", datum.label);
    }
    applyFiltersAndSort();
  }

  function embedChart(key, spec) {
    var el = document.querySelector('[data-threads-chart="' + key + '"]');
    if (!el || !spec || !window.vegaEmbed) {
      return;
    }
    var previous = chartEmbeds[key];
    if (previous) {
      previous.finalize();
      delete chartEmbeds[key];
    }
    var width = el.clientWidth || el.getBoundingClientRect().width || 300;
    var resolvedSpec = spec.facet
      ? applyFacetColumns(spec, width)
      : Object.assign({}, spec, { width: width });
    window
      .vegaEmbed(el, resolvedSpec, { actions: false, renderer: "svg" })
      .then(function (result) {
        chartEmbeds[key] = result;
        result.view.addEventListener("click", function (evt, item) {
          if (item && item.datum) {
            onChartClick(key, item.datum);
          }
        });
      })
      .catch(function () {
        // A chart failing to render must never break the table below.
      });
  }

  function renderCharts() {
    var rows = state.filtered;

    if (chartBaseSpecs.outcome) {
      var outcomeSpec = JSON.parse(JSON.stringify(chartBaseSpecs.outcome));
      outcomeSpec.data = { values: yearOutcomeRows(rows) };
      if (chartShowShare) {
        outcomeSpec.encoding.y.stack = "normalize";
        outcomeSpec.encoding.y.title = "Share of year's threads";
        outcomeSpec.encoding.y.axis = Object.assign({}, outcomeSpec.encoding.y.axis, {
          format: "%",
        });
      }
      embedChart("outcome", outcomeSpec);
    }
    if (chartBaseSpecs["label-constructive"]) {
      var constructiveSpec = JSON.parse(JSON.stringify(chartBaseSpecs["label-constructive"]));
      constructiveSpec.data = { values: labelYearShareRows(rows, CONSTRUCTIVE_LABELS) };
      embedChart("label-constructive", constructiveSpec);
    }
    if (chartBaseSpecs["label-negative"]) {
      var negativeSpec = JSON.parse(JSON.stringify(chartBaseSpecs["label-negative"]));
      negativeSpec.data = { values: labelYearShareRows(rows, NEGATIVE_LABELS) };
      embedChart("label-negative", negativeSpec);
    }
  }

  var outcomeToggleBtn = document.querySelector("[data-threads-outcome-toggle]");
  if (outcomeToggleBtn) {
    outcomeToggleBtn.addEventListener("click", function () {
      chartShowShare = !chartShowShare;
      outcomeToggleBtn.setAttribute("aria-pressed", chartShowShare ? "true" : "false");
      outcomeToggleBtn.textContent = chartShowShare ? "Show counts" : "Show share of year";
      renderCharts();
    });
  }

  var state = {
    rows: [],
    filtered: [],
    page: 0,
    sortField: section.getAttribute("data-default-sort") || "date",
    sortDir: section.getAttribute("data-default-dir") || "desc",
  };

  function escapeHtml(value) {
    return String(value == null ? "" : value).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }

  function fetchJson(url) {
    return fetch(url).then(function (resp) {
      if (!resp.ok) {
        throw new Error("request failed: " + resp.status);
      }
      return resp.json();
    });
  }

  // --- URL state (filters + sort kept in the query string) ----------------

  function readUrlState() {
    var params = new URLSearchParams(window.location.search);
    filterInputs.forEach(function (el) {
      var key = el.getAttribute("data-threads-filter");
      if (params.has(key)) {
        el.value = params.get(key);
      }
    });
    if (params.has("sort")) {
      state.sortField = params.get("sort");
    }
    if (params.has("dir")) {
      state.sortDir = params.get("dir");
    }
  }

  function writeUrlState() {
    var params = new URLSearchParams();
    filterInputs.forEach(function (el) {
      var key = el.getAttribute("data-threads-filter");
      if (el.value) {
        params.set(key, el.value);
      }
    });
    params.set("sort", state.sortField);
    params.set("dir", state.sortDir);
    var newUrl = window.location.pathname + "?" + params.toString();
    window.history.replaceState(null, "", newUrl);
  }

  // --- Filtering -------------------------------------------------------------

  function currentFilters() {
    var values = {};
    filterInputs.forEach(function (el) {
      values[el.getAttribute("data-threads-filter")] = el.value.trim();
    });
    return values;
  }

  function rowMatches(row, f) {
    if (f.venue && row.venue !== f.venue) {
      return false;
    }
    if (f.outcome) {
      // Match on the individual boolean flag, not the composed
      // `outcome_display` string -- filtering for "escalated" must also
      // find a thread whose display reads "escalated, then de-escalated"
      // (issue #122 fixup: outcome flags aren't mutually exclusive).
      var flagMatch =
        f.outcome === "none" ? row.outcome_display === "none" : row.outcome_flags[f.outcome];
      if (!flagMatch) {
        return false;
      }
    }
    if (f.label && !(row.label_counts && row.label_counts[f.label] > 0)) {
      return false;
    }
    if (f.year_from && row.year && row.year < f.year_from) {
      return false;
    }
    if (f.year_to && row.year && row.year > f.year_to) {
      return false;
    }
    return true;
  }

  // --- Sorting -----------------------------------------------------------------

  var SORT_ACCESSORS = {
    date: function (r) {
      return r.date;
    },
    venue: function (r) {
      return r.venue_label;
    },
    n_messages: function (r) {
      return r.n_messages;
    },
    n_distinct_participants: function (r) {
      return r.n_distinct_participants;
    },
    outcome_display: function (r) {
      return r.outcome_display;
    },
    peak_intensity_tier: function (r) {
      return r.peak_intensity_tier;
    },
  };

  function applySort(rows) {
    var accessor = SORT_ACCESSORS[state.sortField] || SORT_ACCESSORS.date;
    var dir = state.sortDir === "asc" ? 1 : -1;
    return rows.slice().sort(function (a, b) {
      var av = accessor(a);
      var bv = accessor(b);
      if (av < bv) return -1 * dir;
      if (av > bv) return 1 * dir;
      return 0;
    });
  }

  function updateSortHeaders() {
    sortHeaders.forEach(function (th) {
      var field = th.getAttribute("data-threads-sort");
      if (field === state.sortField) {
        th.setAttribute("aria-sort", state.sortDir === "asc" ? "ascending" : "descending");
      } else {
        th.setAttribute("aria-sort", "none");
      }
    });
  }

  // --- Rendering -----------------------------------------------------------

  function flaggedCellHtml(row) {
    if (!row.flagged || !row.flagged.length) {
      return "—";
    }
    return row.flagged
      .map(function (f) {
        return (
          '<span class="thread-flag" title="' +
          escapeHtml(f.display) +
          '">' +
          escapeHtml(f.short) +
          ":" +
          f.count +
          "</span>"
        );
      })
      .join(" ");
  }

  function rowHtml(row) {
    var outcome = row.outcome_display + (row.pile_on ? " (pile-on)" : "");
    return (
      "<tr>" +
      '<td data-label="Date" class="thread-nowrap">' + escapeHtml(row.date) + "</td>" +
      '<td data-label="Venue">' + escapeHtml(row.venue_label) + "</td>" +
      '<td data-label="Thread"><a href="' +
      escapeHtml(row.url) +
      '">' +
      escapeHtml(row.subject) +
      "</a></td>" +
      '<td data-label="Messages">' + escapeHtml(row.n_messages) + "</td>" +
      '<td data-label="Participants">' + escapeHtml(row.n_distinct_participants) + "</td>" +
      '<td data-label="Outcome">' + escapeHtml(outcome) + "</td>" +
      '<td data-label="Peak intensity" title="' +
      escapeHtml(row.peak_intensity_label) +
      '">' +
      escapeHtml(row.peak_intensity_tier) +
      "</td>" +
      '<td data-label="Flagged messages">' + flaggedCellHtml(row) + "</td>" +
      '<td data-label="Feedback"><a href="' +
      escapeHtml(row.disagree_url) +
      '">Disagree with this score?</a></td>' +
      "</tr>"
    );
  }

  function render() {
    var total = state.filtered.length;
    var pageCount = Math.max(1, Math.ceil(total / PAGE_SIZE));
    state.page = Math.min(state.page, pageCount - 1);
    var start = state.page * PAGE_SIZE;
    var pageRows = state.filtered.slice(start, start + PAGE_SIZE);

    tbody.innerHTML = pageRows.map(rowHtml).join("");
    if (pageLabel) {
      pageLabel.textContent =
        "Page " + (state.page + 1) + " of " + pageCount + " (" + total + " thread(s))";
    }
    if (prevBtn) prevBtn.disabled = state.page <= 0;
    if (nextBtn) nextBtn.disabled = state.page >= pageCount - 1;

    statusEl.textContent =
      "Loaded " + state.rows.length + " thread(s); " + total + " match the current filters.";
    updateSortHeaders();
  }

  function applyFiltersAndSort() {
    var filters = currentFilters();
    var matching = state.rows.filter(function (row) {
      return rowMatches(row, filters);
    });
    state.filtered = applySort(matching);
    state.page = 0;
    writeUrlState();
    render();
    renderCharts();
  }

  // --- Sorting interaction (mouse + keyboard, per aria-sort) ------------------

  function toggleSort(field) {
    if (state.sortField === field) {
      state.sortDir = state.sortDir === "asc" ? "desc" : "asc";
    } else {
      state.sortField = field;
      state.sortDir = "asc";
    }
    applyFiltersAndSort();
  }

  sortHeaders.forEach(function (th) {
    var field = th.getAttribute("data-threads-sort");
    th.addEventListener("click", function () {
      toggleSort(field);
    });
    th.addEventListener("keydown", function (evt) {
      if (evt.key === "Enter" || evt.key === " " || evt.key === "Spacebar") {
        evt.preventDefault();
        toggleSort(field);
      }
    });
  });

  // --- Filters -----------------------------------------------------------------

  var typingTimer = null;
  filterInputs.forEach(function (el) {
    el.addEventListener("change", applyFiltersAndSort);
    if (el.type === "text") {
      el.addEventListener("input", function () {
        clearTimeout(typingTimer);
        typingTimer = setTimeout(applyFiltersAndSort, 200);
      });
    }
  });

  // --- Pagination -----------------------------------------------------------

  if (prevBtn) {
    prevBtn.addEventListener("click", function () {
      if (state.page > 0) {
        state.page -= 1;
        render();
      }
    });
  }
  if (nextBtn) {
    nextBtn.addEventListener("click", function () {
      state.page += 1;
      render();
    });
  }

  // --- Export (CSV/JSON of the currently filtered set) --------------------------

  var CSV_COLUMNS = [
    "date",
    "venue",
    "subject",
    "url",
    "n_messages",
    "n_distinct_participants",
    "outcome_display",
    "escalated",
    "de_escalated",
    "resolved",
    "abandoned_after_friction",
    "pile_on",
    "peak_intensity_tier",
  ];

  function csvEscape(value) {
    var s = String(value == null ? "" : value);
    if (/[",\n]/.test(s)) {
      return '"' + s.replace(/"/g, '""') + '"';
    }
    return s;
  }

  function rowToCsvValues(row) {
    return [
      row.date,
      row.venue_label,
      row.subject,
      row.url,
      row.n_messages,
      row.n_distinct_participants,
      row.outcome_display,
      row.outcome_flags.escalated,
      row.outcome_flags["de-escalated"],
      row.outcome_flags.resolved,
      row.outcome_flags["abandoned after friction"],
      row.pile_on,
      row.peak_intensity_tier,
    ];
  }

  function download(filename, contents, mime) {
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

  function exportCsv() {
    var lines = [CSV_COLUMNS.map(csvEscape).join(",")];
    state.filtered.forEach(function (row) {
      lines.push(rowToCsvValues(row).map(csvEscape).join(","));
    });
    download("conversation-threads-filtered.csv", lines.join("\n"), "text/csv");
  }

  function exportJson() {
    download(
      "conversation-threads-filtered.json",
      JSON.stringify({ row_count: state.filtered.length, rows: state.filtered }, null, 0),
      "application/json"
    );
  }

  exportButtons.forEach(function (btn) {
    btn.addEventListener("click", function () {
      var kind = btn.getAttribute("data-threads-export");
      if (kind === "csv") {
        exportCsv();
      } else {
        exportJson();
      }
    });
  });

  // Re-resolve each chart's width on viewport/layout changes -- these chart
  // containers aren't `[data-vega-spec]` elements (see the top of this
  // file), so `app.js`'s own resize handler never touches them; same
  // reasoning/timer debounce as `app.js`'s own listener.
  var chartResizeTimer = null;
  window.addEventListener("resize", function () {
    if (chartResizeTimer) {
      clearTimeout(chartResizeTimer);
    }
    chartResizeTimer = setTimeout(renderCharts, 150);
  });

  // --- Boot -----------------------------------------------------------------

  readUrlState();

  fetchJson(section.getAttribute("data-rows-href"))
    .then(function (payload) {
      state.rows = payload.rows || [];
      applyFiltersAndSort();
    })
    .catch(function () {
      statusEl.textContent = "Could not load thread data.";
    });
})();
