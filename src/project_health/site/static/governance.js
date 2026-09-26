(function () {
  "use strict";

  // Per-commit table: fetches the governance page's JSON data file(s)
  // (`site/governance_page.py`'s `governance-commits-recent.json` /
  // `-full.json`), then filters/paginates entirely client-side (issue #37).
  // Only the *recent* file (last N months) is fetched on load, so a project
  // with 32k+ scored commits never ships that whole history to a first
  // paint; "load full history" is an explicit, on-demand fetch of the
  // larger file.

  var PAGE_SIZE = 50;

  var section = document.querySelector("[data-gov-commits]");
  if (!section) {
    return;
  }

  var table = section.querySelector("[data-gov-table]");
  var tbody = section.querySelector("[data-gov-tbody]");
  var statusEl = section.querySelector("[data-gov-status]");
  var pageLabel = section.querySelector("[data-gov-page-label]");
  var prevBtn = section.querySelector("[data-gov-prev]");
  var nextBtn = section.querySelector("[data-gov-next]");
  var loadAllBtn = section.querySelector("[data-gov-load-all]");
  var filterInputs = Array.prototype.slice.call(section.querySelectorAll("[data-gov-filter]"));

  var checkIds = (table.getAttribute("data-gov-checks") || "")
    .split(",")
    .map(function (s) {
      return s.trim();
    })
    .filter(Boolean);

  var state = {
    rows: [],
    filtered: [],
    page: 0,
    loadedScope: null, // "recent" | "full"
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

  function monthOf(isoDate) {
    return isoDate ? isoDate.slice(0, 7) : "";
  }

  function currentFilters() {
    var values = {};
    filterInputs.forEach(function (el) {
      values[el.getAttribute("data-gov-filter")] = el.value;
    });
    return values;
  }

  function rowMatches(row, filters) {
    if (filters.month && monthOf(row.commit_date) !== filters.month) {
      return false;
    }
    if (filters.branch && row.branch !== filters.branch) {
      return false;
    }
    if (filters.check) {
      var check = row.checks[filters.check];
      if (!check) {
        return false;
      }
      if (filters.result && check.result !== filters.result) {
        return false;
      }
    } else if (filters.result) {
      var anyMatches = Object.keys(row.checks).some(function (id) {
        return row.checks[id].result === filters.result;
      });
      if (!anyMatches) {
        return false;
      }
    }
    return true;
  }

  function applyFilters() {
    var filters = currentFilters();
    state.filtered = state.rows.filter(function (row) {
      return rowMatches(row, filters);
    });
    state.page = 0;
    render();
  }

  function checkCellHtml(row, checkId) {
    var check = row.checks[checkId];
    if (!check) {
      return '<td class="gov-result gov-result--na">n/a</td>';
    }
    var title = escapeHtml(check.evidence || "");
    var evidenceLink = check.evidence_url
      ? ' <a href="' + escapeHtml(check.evidence_url) + '">evidence</a>'
      : "";
    return (
      '<td class="gov-result gov-result--' +
      escapeHtml(check.result) +
      '" title="' +
      title +
      '">' +
      escapeHtml(check.result) +
      evidenceLink +
      "</td>"
    );
  }

  function rowHtml(row) {
    var jira = row.jira_keys
      .map(function (key, i) {
        return '<a href="' + escapeHtml(row.jira_urls[i]) + '">' + escapeHtml(key) + "</a>";
      })
      .join(", ");
    var reviewers = row.reviewers.length ? escapeHtml(row.reviewers.join(", ")) : "—";
    var date = row.commit_date ? row.commit_date.slice(0, 10) : "";
    var cells = [
      '<td><a href="' + escapeHtml(row.commit_url) + '"><code>' + escapeHtml(row.short_sha) + "</code></a></td>",
      "<td>" + escapeHtml(date) + "</td>",
      "<td>" + escapeHtml(row.branch) + "</td>",
      "<td>" + (jira || "—") + "</td>",
      "<td>" + escapeHtml(row.author) + "</td>",
      "<td>" + escapeHtml(row.committer) + "</td>",
      "<td>" + reviewers + "</td>",
    ];
    checkIds.forEach(function (id) {
      cells.push(checkCellHtml(row, id));
    });
    cells.push('<td><a href="' + escapeHtml(row.correction_url) + '">correct</a></td>');
    return "<tr>" + cells.join("") + "</tr>";
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

    var scopeLabel = state.loadedScope === "full" ? "full history" : "last months";
    statusEl.textContent =
      "Loaded " + state.rows.length + " commit(s) (" + scopeLabel + "); " + total + " match the current filters.";
  }

  function loadScope(url, scope) {
    statusEl.textContent = "Loading commits…";
    return fetchJson(url).then(function (payload) {
      state.rows = payload.rows || [];
      state.loadedScope = scope;
      applyFilters();
    });
  }

  filterInputs.forEach(function (el) {
    el.addEventListener("change", applyFilters);
  });

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

  if (loadAllBtn) {
    loadAllBtn.addEventListener("click", function () {
      loadAllBtn.disabled = true;
      loadAllBtn.textContent = "Loading full history…";
      loadScope(section.getAttribute("data-full-href"), "full")
        .then(function () {
          loadAllBtn.textContent = "Full history loaded";
        })
        .catch(function () {
          loadAllBtn.disabled = false;
          loadAllBtn.textContent = "load full history";
          statusEl.textContent = "Could not load full history — try again, or use the CSV download.";
        });
    });
  }

  loadScope(section.getAttribute("data-recent-href"), "recent").catch(function () {
    statusEl.textContent = "Could not load commit data — use the CSV/JSON downloads instead.";
  });
})();
