(function () {
  function getBuildStamp() {
    var script = document.querySelector(
      'script[src*="docs-footer-badge.js"][data-docs-built-at]'
    );
    if (script) {
      var stamp = script.getAttribute("data-docs-built-at");
      if (stamp) return stamp;
    }
    return "";
  }

  function mountFooterStamp() {
    var stamp = getBuildStamp();
    if (!stamp) return;
    if (document.getElementById("simstack-docs-built-at")) return;

    var container =
      document.querySelector(".bottom-of-page .left-details") ||
      document.querySelector(".bottom-of-page") ||
      document.querySelector("footer");
    if (!container) return;

    var line = document.createElement("div");
    line.id = "simstack-docs-built-at";
    line.textContent = "Docs build: " + stamp;
    line.style.marginTop = "0.35rem";
    line.style.fontSize = "0.8rem";
    line.style.opacity = "0.85";
    container.appendChild(line);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", mountFooterStamp);
  } else {
    mountFooterStamp();
  }
})();
