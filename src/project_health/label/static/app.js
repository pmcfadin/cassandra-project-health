// project-health label -- vanilla JS, no CDN, works fully offline against
// the local server (DECISIONS.md D18, D22; issue #46; gap-focused
// label_set mode, issue #90, DECISIONS.md D23).
//
// Blind by construction: the state this script renders never includes a
// `stratum` field or any classifier/Jev output -- the server's
// `/api/state` response simply does not contain them (see server.py's
// `build_state_payload`), so there is nothing for this file to hide.
//
// Every label defaults to "no" and tone defaults to 0 (issue #90's speedup):
// both the main form and the compact quick-check row pre-check "no" as soon
// as they're rendered, and the Space shortcut (or the "Nothing applies"
// button) saves those defaults immediately and moves to the next item,
// recording `defaults_applied: true` on that record so the reports can flag
// it as a lower-confidence, unreviewed "no" (pilot/report.py).
"use strict";

(function () {
  var state = null; // last /api/state payload
  var currentRow = 0; // keyboard-highlighted label row index (main form only)
  var itemLoadedAt = null; // Date.now() when the current item was rendered
  var marks = {}; // { labelId: "yes" | "no" | "unsure" }
  var defaultsShortcutUsed = false; // true only for the save that just used Space/the button

  var labelRowsEl = document.getElementById("label-rows");
  var quickCheckFieldsetEl = document.getElementById("quick-check-fieldset");
  var quickCheckRowsEl = document.getElementById("quick-check-rows");
  var definitionsListEl = document.getElementById("definitions-list");
  var form = document.getElementById("label-form");
  var saveErrorEl = document.getElementById("save-error");
  var defaultsBtn = document.getElementById("defaults-btn");

  function qs(id) {
    return document.getElementById(id);
  }

  function renderToneOptions(toneLevels) {
    var container = qs("tone-options");
    container.innerHTML = "";
    toneLevels.forEach(function (level) {
      var wrapper = document.createElement("label");
      wrapper.className = "tone-option";
      var input = document.createElement("input");
      input.type = "radio";
      input.name = "tone";
      input.id = "tone-" + level.level;
      input.value = String(level.level);
      if (level.level === 0) {
        input.checked = true; // tone defaults to 0 (issue #90)
      }
      wrapper.appendChild(input);
      var text = document.createElement("span");
      text.textContent = " " + level.level + " (" + level.name + "): " + level.description;
      wrapper.appendChild(text);
      container.appendChild(wrapper);
    });
  }

  function renderDefinitions(labels, quickCheckLabels) {
    definitionsListEl.innerHTML = "";
    (labels || []).concat(quickCheckLabels || []).forEach(function (label) {
      var dt = document.createElement("dt");
      dt.textContent = "#" + label.number + " " + label.id;
      var dd = document.createElement("dd");
      dd.textContent = label.definition;
      definitionsListEl.appendChild(dt);
      definitionsListEl.appendChild(dd);
    });
  }

  // Builds one label row (full-size for the main form, compact for the
  // quick-check row -- `compact` only changes CSS class, not behavior:
  // both default to "no" and both are required, presented labels).
  function buildLabelRow(label, index, compact) {
    var row = document.createElement("div");
    row.className = compact ? "label-row label-row-compact" : "label-row";
    row.dataset.labelId = label.id;
    row.dataset.index = String(index);

    var heading = document.createElement("div");
    heading.className = "label-row-heading";
    heading.textContent = "#" + label.number + " " + label.id;
    row.appendChild(heading);

    if (!compact) {
      var def = document.createElement("div");
      def.className = "label-row-definition";
      def.textContent = label.definition;
      row.appendChild(def);
    }

    var choices = document.createElement("div");
    choices.className = "label-row-choices";
    ["yes", "no", "unsure"].forEach(function (value) {
      var id = "label-" + label.id + "-" + value;
      var wrapper = document.createElement("label");
      var input = document.createElement("input");
      input.type = "radio";
      input.name = "label-" + label.id;
      input.id = id;
      input.value = value;
      if (value === "no") {
        input.checked = true; // every label defaults to "no" (issue #90)
      }
      input.addEventListener("change", function () {
        marks[label.id] = value;
      });
      wrapper.appendChild(input);
      wrapper.appendChild(document.createTextNode(" " + value));
      choices.appendChild(wrapper);
    });
    row.appendChild(choices);
    marks[label.id] = "no"; // matches the pre-checked "no" radio above
    return row;
  }

  function renderLabelRows(labels) {
    labelRowsEl.innerHTML = "";
    labels.forEach(function (label, index) {
      labelRowsEl.appendChild(buildLabelRow(label, index, false));
    });
    highlightRow(0);
  }

  function renderQuickCheckRows(quickCheckLabels) {
    quickCheckRowsEl.innerHTML = "";
    if (!quickCheckLabels || quickCheckLabels.length === 0) {
      quickCheckFieldsetEl.hidden = true;
      return;
    }
    quickCheckFieldsetEl.hidden = false;
    quickCheckLabels.forEach(function (label, index) {
      quickCheckRowsEl.appendChild(buildLabelRow(label, index, true));
    });
  }

  function highlightRow(index) {
    var rows = labelRowsEl.querySelectorAll(".label-row");
    if (rows.length === 0) {
      return;
    }
    currentRow = Math.max(0, Math.min(index, rows.length - 1));
    rows.forEach(function (row, i) {
      row.classList.toggle("current", i === currentRow);
    });
  }

  function setCurrentRowMark(value) {
    var rows = labelRowsEl.querySelectorAll(".label-row");
    var row = rows[currentRow];
    if (!row) {
      return;
    }
    var labelId = row.dataset.labelId;
    var input = qs("label-" + labelId + "-" + value);
    if (input) {
      input.checked = true;
      marks[labelId] = value;
    }
  }

  function setTone(value) {
    var input = qs("tone-" + value);
    if (input) {
      input.checked = true;
    }
  }

  function renderItem(payload) {
    var completeSection = qs("complete-message");
    var labelingSection = qs("labeling-area");
    if (payload.complete || !payload.item) {
      completeSection.hidden = false;
      labelingSection.hidden = true;
      return;
    }
    completeSection.hidden = true;
    labelingSection.hidden = false;

    var item = payload.item;
    qs("item-source").textContent = item.source;
    qs("item-text").textContent = item.text;
    var parentWrap = qs("item-parent-wrap");
    if (item.parent_text) {
      parentWrap.hidden = false;
      qs("item-parent-text").textContent = item.parent_text;
    } else {
      parentWrap.hidden = true;
      qs("item-parent-text").textContent = "";
    }
    var link = qs("item-archive-link");
    link.href = item.archive_url;

    form.reset();
    saveErrorEl.textContent = "";
    defaultsShortcutUsed = false;
    itemLoadedAt = Date.now();
  }

  function renderProgress(progress) {
    qs("progress-text").textContent = progress.done + " / " + progress.total;
    var pct = progress.total === 0 ? 0 : (100 * progress.done) / progress.total;
    qs("progress-bar").style.width = pct + "%";
  }

  function render(payload) {
    state = payload;
    qs("rater-name").textContent = payload.rater;
    qs("label-set-name").textContent = payload.label_set || "full";
    renderProgress(payload.progress);
    renderDefinitions(payload.labels, payload.quick_check_labels);
    renderLabelRows(payload.labels);
    renderQuickCheckRows(payload.quick_check_labels);
    renderToneOptions(payload.tone_levels);
    renderItem(payload);
  }

  function loadState() {
    return fetch("/api/state")
      .then(function (resp) {
        return resp.json();
      })
      .then(render);
  }

  function submitLabels() {
    if (!state || !state.item) {
      return;
    }
    var toneInput = form.querySelector('input[name="tone"]:checked');
    var body = {
      item_id: state.item.id,
      labels: marks,
      tone: toneInput ? parseInt(toneInput.value, 10) : null,
      note: qs("note").value || null,
      seconds: itemLoadedAt ? (Date.now() - itemLoadedAt) / 1000 : 0,
      defaults_applied: defaultsShortcutUsed,
    };
    fetch("/api/save", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    })
      .then(function (resp) {
        return resp.json().then(function (payload) {
          return { ok: resp.ok, payload: payload };
        });
      })
      .then(function (result) {
        if (!result.ok) {
          saveErrorEl.textContent = result.payload.error || "save failed";
          return;
        }
        render(result.payload);
      });
  }

  // "Nothing applies" (issue #90): a genuine reset-and-save, not just "save
  // whatever's currently checked" -- every presented label (main + quick
  // check) is forced back to "no" and tone back to 0 even if the rater had
  // already changed some of them, because "nothing applies" means exactly
  // that: none of the presented labels apply to this item, full stop.
  function applyDefaultsAndSubmit() {
    if (!state || !state.item) {
      return;
    }
    var allLabels = (state.labels || []).concat(state.quick_check_labels || []);
    allLabels.forEach(function (label) {
      var input = qs("label-" + label.id + "-no");
      if (input) {
        input.checked = true;
      }
      marks[label.id] = "no";
    });
    setTone(0);
    defaultsShortcutUsed = true;
    submitLabels();
  }

  form.addEventListener("submit", function (event) {
    event.preventDefault();
    defaultsShortcutUsed = false;
    submitLabels();
  });

  defaultsBtn.addEventListener("click", function (event) {
    event.preventDefault();
    applyDefaultsAndSubmit();
  });

  document.addEventListener("keydown", function (event) {
    var active = document.activeElement;
    var inTextarea = active && active.tagName === "TEXTAREA";
    if (inTextarea && event.key !== "Enter") {
      return; // let the rater type freely in the note field
    }
    switch (event.key) {
      case "ArrowDown":
        event.preventDefault();
        highlightRow(currentRow + 1);
        break;
      case "ArrowUp":
        event.preventDefault();
        highlightRow(currentRow - 1);
        break;
      case "y":
        setCurrentRowMark("yes");
        break;
      case "n":
        setCurrentRowMark("no");
        break;
      case "u":
        setCurrentRowMark("unsure");
        break;
      case " ":
      case "Spacebar": // older browsers
        event.preventDefault();
        applyDefaultsAndSubmit();
        break;
      case "0":
      case "1":
      case "2":
      case "3":
      case "4":
      case "5":
      case "6":
      case "7":
      case "8":
      case "9":
        setTone(event.key);
        break;
      case "Enter":
        if (!inTextarea) {
          event.preventDefault();
          defaultsShortcutUsed = false;
          submitLabels();
        }
        break;
      default:
        break;
    }
  });

  loadState();
})();
