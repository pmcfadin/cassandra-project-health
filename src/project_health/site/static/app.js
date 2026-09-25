(function () {
  "use strict";

  // Views currently embedded, keyed by their container element, so a
  // re-render (window resize) finalizes the old view before replacing it
  // instead of stacking a second <svg> inside the same container.
  var embeddedResults = new WeakMap();

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
