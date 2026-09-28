(function () {
  "use strict";

  // Commit-history table (issue #97): fetches the default-range commit
  // facts file (from 2020-06-25 on), lazily fetches the older-history file
  // only when that toggle is switched on, then filters/sorts/paginates
  // entirely client-side. Every row is a *fact*, never a pass/fail verdict
  // (see `site/governance_page.py`'s module docstring) -- this file renders
  // exactly the text/bucket values the server already computed, and never
  // recomputes or re-labels them as compliant/non-compliant.

  var PAGE_SIZE = 50;

  var section = document.querySelector("[data-gov-commits]");
  if (!section) {
    return;
  }

  var tbody = section.querySelector("[data-gov-tbody]");
  var statusEl = section.querySelector("[data-gov-status]");
  var pageLabel = section.querySelector("[data-gov-page-label]");
  var prevBtn = section.querySelector("[data-gov-prev]");
  var nextBtn = section.querySelector("[data-gov-next]");
  var olderToggle = section.querySelector("[data-gov-older-toggle]");
  var mergesToggle = section.querySelector("[data-gov-merges-toggle]");
  var exportButtons = Array.prototype.slice.call(section.querySelectorAll("[data-gov-export]"));
  var filterInputs = Array.prototype.slice.call(section.querySelectorAll("[data-gov-filter]"));
  var sortHeaders = Array.prototype.slice.call(section.querySelectorAll("[data-gov-sort]"));

  var CHECKBOX_FILTERS = ["docs_only", "release_process", "ninja", "changes_txt", "news_txt"];

  var state = {
    defaultRows: [],
    olderRows: [],
    olderLoaded: false,
    filtered: [],
    page: 0,
    sortField: "commit_date",
    sortDir: "desc",
    expanded: {},
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

  // --- URL state (filters + sort + toggles kept in the query string, so a
  // view can be shared) --------------------------------------------------

  function readUrlState() {
    var params = new URLSearchParams(window.location.search);
    filterInputs.forEach(function (el) {
      var key = el.getAttribute("data-gov-filter");
      if (!params.has(key)) {
        return;
      }
      if (el.type === "checkbox") {
        el.checked = params.get(key) === "1";
      } else {
        el.value = params.get(key);
      }
    });
    if (params.has("sort")) {
      state.sortField = params.get("sort");
    }
    if (params.has("dir")) {
      state.sortDir = params.get("dir");
    }
    if (params.get("merges") === "1" && mergesToggle) {
      mergesToggle.checked = true;
    }
    if (params.get("older") === "1" && olderToggle) {
      olderToggle.checked = true;
    }
  }

  function writeUrlState() {
    var params = new URLSearchParams();
    filterInputs.forEach(function (el) {
      var key = el.getAttribute("data-gov-filter");
      if (el.type === "checkbox") {
        if (el.checked) {
          params.set(key, "1");
        }
      } else if (el.value) {
        params.set(key, el.value);
      }
    });
    params.set("sort", state.sortField);
    params.set("dir", state.sortDir);
    if (mergesToggle && mergesToggle.checked) {
      params.set("merges", "1");
    }
    if (olderToggle && olderToggle.checked) {
      params.set("older", "1");
    }
    var newUrl = window.location.pathname + "?" + params.toString();
    window.history.replaceState(null, "", newUrl);
  }

  // --- Filtering -----------------------------------------------------------

  function currentFilters() {
    var values = {};
    filterInputs.forEach(function (el) {
      var key = el.getAttribute("data-gov-filter");
      values[key] = el.type === "checkbox" ? el.checked : el.value.trim();
    });
    values._showMerges = !!(mergesToggle && mergesToggle.checked);
    return values;
  }

  function rowMatches(row, f) {
    if (!f._showMerges && row.is_merge) {
      return false;
    }
    if (f.branch && row.branch !== f.branch) {
      return false;
    }
    if (f.reviewer === "yes" && !row.reviewer.named) {
      return false;
    }
    if (f.reviewer === "no" && row.reviewer.named) {
      return false;
    }
    if (f.ci_evidence && row.ci_evidence.bucket !== f.ci_evidence) {
      return false;
    }
    if (f.ci_artefacts && row.ci_artefacts.bucket !== f.ci_artefacts) {
      return false;
    }
    if (f.checkstyle && row.checkstyle.bucket !== f.checkstyle) {
      return false;
    }
    if (f.date_from && row.commit_date.slice(0, 10) < f.date_from) {
      return false;
    }
    if (f.date_to && row.commit_date.slice(0, 10) > f.date_to) {
      return false;
    }
    if (f.author && row.author.toLowerCase().indexOf(f.author.toLowerCase()) === -1) {
      return false;
    }
    if (f.search) {
      var needle = f.search.toLowerCase();
      var haystack = (
        row.sha + " " + row.jira_keys.join(" ") + " " + (row.subject || "")
      ).toLowerCase();
      if (haystack.indexOf(needle) === -1) {
        return false;
      }
    }
    for (var i = 0; i < CHECKBOX_FILTERS.length; i++) {
      var key = CHECKBOX_FILTERS[i];
      if (!f[key]) {
        continue;
      }
      var actual =
        key === "changes_txt"
          ? row.changes_txt_touched
          : key === "news_txt"
            ? row.news_txt_touched
            : row.tags[key];
      if (!actual) {
        return false;
      }
    }
    return true;
  }

  // --- Sorting ---------------------------------------------------------------

  var SORT_ACCESSORS = {
    commit_date: function (r) {
      return r.commit_date;
    },
    sha: function (r) {
      return r.sha;
    },
    jira: function (r) {
      return r.jira_keys.join(",");
    },
    reviewer: function (r) {
      return r.reviewer.text;
    },
    ci_evidence: function (r) {
      return r.ci_evidence.bucket + " " + (r.ci_evidence.text || "");
    },
    ci_artefacts: function (r) {
      return r.ci_artefacts.bucket + " " + (r.ci_artefacts.text || "");
    },
    checkstyle: function (r) {
      return r.checkstyle.bucket + " " + (r.checkstyle.text || "");
    },
    changes_txt: function (r) {
      return r.changes_txt_touched ? 1 : 0;
    },
    news_txt: function (r) {
      return r.news_txt_touched ? 1 : 0;
    },
  };

  function applySort(rows) {
    var accessor = SORT_ACCESSORS[state.sortField] || SORT_ACCESSORS.commit_date;
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
      var field = th.getAttribute("data-gov-sort");
      if (field === state.sortField) {
        th.setAttribute("aria-sort", state.sortDir === "asc" ? "ascending" : "descending");
      } else {
        th.setAttribute("aria-sort", "none");
      }
    });
  }

  // --- Rendering ---------------------------------------------------------------

  function allRows() {
    var rows = state.defaultRows;
    if (olderToggle && olderToggle.checked && state.olderLoaded) {
      rows = rows.concat(state.olderRows);
    }
    return rows;
  }

  // Compact cell text (the full sentence stays in the title tooltip and the
  // expanded evidence trail), so the whole table fits a normal window.
  function shortLead(seconds) {
    var s = Math.abs(seconds || 0);
    if (s < 3600) return Math.max(1, Math.round(s / 60)) + " min";
    if (s < 86400) return Math.max(1, Math.round(s / 3600)) + " h";
    var d = Math.round(s / 86400);
    return d + (d === 1 ? " day" : " days");
  }

  var BUCKET_LABELS = {
    none: "none found",
    not_checked: "not checked",
    no_ticket: "no ticket",
  };

  function shortCiEvidence(fact) {
    if (BUCKET_LABELS[fact.bucket]) return BUCKET_LABELS[fact.bucket];
    var kind = /^attachment/i.test(fact.text || "") ? "attachment" : "comment";
    var when = fact.lead_time_seconds != null ? " · " + shortLead(fact.lead_time_seconds) : "";
    return kind + when + (fact.bucket === "after" ? " after" : " before");
  }

  function shortCiArtefacts(fact) {
    if (BUCKET_LABELS[fact.bucket]) return BUCKET_LABELS[fact.bucket];
    if (fact.bucket === "both") {
      var m = /, (.+?) (before|after) commit/.exec(fact.text || "");
      return "both" + (m ? " · " + m[1] + " " + m[2] : "");
    }
    return fact.text || "";
  }

  function shortCheckstyle(fact) {
    if (fact.bucket === "success") return "success";
    if (fact.bucket === "failure") return "failure";
    return "no run";
  }

  function factCellHtml(fact, label, shortText) {
    var full = fact.text || "";
    var link = fact.url ? ' <a href="' + escapeHtml(fact.url) + '">link</a>' : "";
    return (
      '<td data-label="' + escapeHtml(label) + '" title="' + escapeHtml(full) + '">' +
      escapeHtml(shortText) + link + "</td>"
    );
  }

  function reviewerCellHtml(reviewer) {
    var names = (reviewer.names || []).map(function (n) { return n.name; });
    var shown = names.length ? names.slice(0, 3).join(", ") : "none named";
    if (names.length > 3) shown += " +" + (names.length - 3);
    return (
      '<td data-label="Reviewed by" title="' + escapeHtml(reviewer.text || "") + '">' +
      escapeHtml(shown) + "</td>"
    );
  }

  function evidenceTrailHtml(row) {
    var parts = [];
    parts.push("<dt>Reviewed by</dt><dd>" + escapeHtml(row.reviewer.text) + "</dd>");
    parts.push(
      "<dt>CI evidence</dt><dd>" +
        escapeHtml(row.ci_evidence.text || "") +
        (row.ci_evidence.evidence_at
          ? " (" + escapeHtml(row.ci_evidence.evidence_at) + ")"
          : "") +
        (row.ci_evidence.url
          ? ' — <a href="' + escapeHtml(row.ci_evidence.url) + '">evidence</a>'
          : "") +
        "</dd>"
    );
    parts.push(
      "<dt>CI artefacts on JIRA</dt><dd>" +
        escapeHtml(row.ci_artefacts.text || "") +
        (row.ci_artefacts.evidence_at
          ? " (" + escapeHtml(row.ci_artefacts.evidence_at) + ")"
          : "") +
        "</dd>"
    );
    parts.push(
      "<dt>Checkstyle</dt><dd>" +
        escapeHtml(row.checkstyle.text || "") +
        (row.checkstyle.url
          ? ' — <a href="' + escapeHtml(row.checkstyle.url) + '">check-run</a>'
          : "") +
        "</dd>"
    );
    parts.push(
      "<dt>CHANGES.txt</dt><dd>" + (row.changes_txt_touched ? "included" : "—") + "</dd>"
    );
    parts.push("<dt>NEWS.txt</dt><dd>" + (row.news_txt_touched ? "included" : "—") + "</dd>");
    var tags = [];
    if (row.tags.docs_only) tags.push("docs-only change");
    if (row.tags.release_process) tags.push("release-process commit");
    if (row.tags.ninja) tags.push('declares "ninja"');
    if (tags.length) {
      parts.push("<dt>Tags</dt><dd>" + tags.map(escapeHtml).join(", ") + "</dd>");
    }
    return '<dl class="gov-evidence-trail">' + parts.join("") + "</dl>";
  }

  function rowHtml(row, index) {
    var jira = row.jira_keys
      .map(function (key, i) {
        return '<a href="' + escapeHtml(row.jira_urls[i]) + '">' + escapeHtml(key) + "</a>";
      })
      .join(", ");
    var date = row.commit_date ? row.commit_date.slice(0, 10) : "";
    var detailId = "gov-detail-" + index;
    var expanded = !!state.expanded[row.sha];
    var cells = [
      '<td class="gov-expand-cell"><button type="button" class="gov-expand-btn" data-gov-expand="' +
        escapeHtml(row.sha) +
        '" aria-expanded="' +
        (expanded ? "true" : "false") +
        '" aria-controls="' +
        detailId +
        '">' +
        (expanded ? "−" : "+") +
        '<span class="visually-hidden"> details</span></button></td>',
      '<td data-label="Date" class="gov-nowrap">' + escapeHtml(date) + "</td>",
      '<td data-label="Commit"><a href="' +
        escapeHtml(row.commit_url) +
        '"><code>' +
        escapeHtml(row.short_sha) +
        "</code></a><br>" +
        escapeHtml(row.subject) +
        '<br><span class="gov-author">' +
        escapeHtml(row.author) +
        "</span></td>",
      '<td data-label="Ticket" class="gov-nowrap">' + (jira || "—") + "</td>",
      reviewerCellHtml(row.reviewer),
    ];
    cells.push(factCellHtml(row.ci_evidence, "CI evidence", shortCiEvidence(row.ci_evidence)));
    cells.push(factCellHtml(row.ci_artefacts, "CI artefacts on JIRA", shortCiArtefacts(row.ci_artefacts)));
    cells.push(factCellHtml(row.checkstyle, "Checkstyle", shortCheckstyle(row.checkstyle)));
    cells.push(
      '<td data-label="CHANGES.txt">' + (row.changes_txt_touched ? "included" : "—") + "</td>"
    );
    cells.push(
      '<td data-label="NEWS.txt">' + (row.news_txt_touched ? "included" : "—") + "</td>"
    );
    var html = "<tr>" + cells.join("") + "</tr>";
    if (expanded) {
      html +=
        '<tr class="gov-row-detail" id="' +
        detailId +
        '"><td colspan="10">' +
        evidenceTrailHtml(row) +
        "</td></tr>";
    }
    return html;
  }

  function render() {
    var total = state.filtered.length;
    var pageCount = Math.max(1, Math.ceil(total / PAGE_SIZE));
    state.page = Math.min(state.page, pageCount - 1);
    var start = state.page * PAGE_SIZE;
    var pageRows = state.filtered.slice(start, start + PAGE_SIZE);

    tbody.innerHTML = pageRows.map(rowHtml).join("");
    pageLabel.textContent = "Page " + (state.page + 1) + " of " + pageCount + " (" + total + " commit row(s))";
    prevBtn.disabled = state.page <= 0;
    nextBtn.disabled = state.page >= pageCount - 1;

    var loadedCount = allRows().length;
    statusEl.textContent = "Loaded " + loadedCount + " commit(s); " + total + " match the current filters.";
    updateSortHeaders();
  }

  function applyFiltersAndSort() {
    var filters = currentFilters();
    var matching = allRows().filter(function (row) {
      return rowMatches(row, filters);
    });
    state.filtered = applySort(matching);
    state.page = 0;
    writeUrlState();
    render();
  }

  // --- Row expansion -----------------------------------------------------------

  tbody.addEventListener("click", function (evt) {
    var btn = evt.target.closest ? evt.target.closest("[data-gov-expand]") : null;
    if (!btn) {
      return;
    }
    var sha = btn.getAttribute("data-gov-expand");
    state.expanded[sha] = !state.expanded[sha];
    render();
  });

  // --- Sorting interaction (mouse + keyboard, per aria-sort) --------------------

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
    var field = th.getAttribute("data-gov-sort");
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

  // --- Filters / toggles ----------------------------------------------------

  filterInputs.forEach(function (el) {
    el.addEventListener("change", applyFiltersAndSort);
  });
  if (mergesToggle) {
    mergesToggle.addEventListener("change", applyFiltersAndSort);
  }

  function loadOlderHistory() {
    if (state.olderLoaded) {
      applyFiltersAndSort();
      return;
    }
    statusEl.textContent = "Loading older history…";
    fetchJson(section.getAttribute("data-older-href"))
      .then(function (payload) {
        state.olderRows = payload.rows || [];
        state.olderLoaded = true;
        applyFiltersAndSort();
      })
      .catch(function () {
        statusEl.textContent = "Could not load older history — try again.";
      });
  }

  if (olderToggle) {
    olderToggle.addEventListener("change", function () {
      if (olderToggle.checked) {
        loadOlderHistory();
      } else {
        applyFiltersAndSort();
      }
    });
  }

  // --- Pagination -----------------------------------------------------------

  prevBtn.addEventListener("click", function () {
    if (state.page > 0) {
      state.page -= 1;
      render();
    }
  });
  nextBtn.addEventListener("click", function () {
    state.page += 1;
    render();
  });

  // --- Export (CSV/JSON of the currently filtered set) --------------------------

  var CSV_COLUMNS = [
    "sha",
    "branch",
    "commit_date",
    "subject",
    "author",
    "committer",
    "jira_keys",
    "reviewer",
    "ci_evidence",
    "ci_artefacts",
    "checkstyle",
    "changes_txt",
    "news_txt",
    "docs_only",
    "release_process",
    "ninja",
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
      row.sha,
      row.branch,
      row.commit_date,
      row.subject,
      row.author,
      row.committer,
      row.jira_keys.join(";"),
      row.reviewer.text,
      row.ci_evidence.bucket + ": " + (row.ci_evidence.text || ""),
      row.ci_artefacts.bucket + ": " + (row.ci_artefacts.text || ""),
      row.checkstyle.bucket + ": " + (row.checkstyle.text || ""),
      row.changes_txt_touched,
      row.news_txt_touched,
      row.tags.docs_only,
      row.tags.release_process,
      row.tags.ninja,
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
    download("governance-commits-filtered.csv", lines.join("\n"), "text/csv");
  }

  function exportJson() {
    download(
      "governance-commits-filtered.json",
      JSON.stringify({ row_count: state.filtered.length, rows: state.filtered }, null, 0),
      "application/json"
    );
  }

  exportButtons.forEach(function (btn) {
    btn.addEventListener("click", function () {
      var kind = btn.getAttribute("data-gov-export");
      if (kind === "csv") {
        exportCsv();
      } else {
        exportJson();
      }
    });
  });

  // --- Boot -----------------------------------------------------------------

  readUrlState();

  var needsOlder = olderToggle && olderToggle.checked;

  fetchJson(section.getAttribute("data-default-href"))
    .then(function (payload) {
      state.defaultRows = payload.rows || [];
      if (needsOlder) {
        return fetchJson(section.getAttribute("data-older-href")).then(function (olderPayload) {
          state.olderRows = olderPayload.rows || [];
          state.olderLoaded = true;
        });
      }
    })
    .then(applyFiltersAndSort)
    .catch(function () {
      statusEl.textContent = "Could not load commit data.";
    });
})();
