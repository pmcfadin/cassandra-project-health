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
