(function () {
    function createBuildBadge() {
        const script =
            document.currentScript ||
            document.querySelector('script[src*="docs-build-badge.js"][data-docs-built-at]');

        if (!script) {
            return;
        }

        const builtAt = script.getAttribute("data-docs-built-at");
        if (!builtAt || document.getElementById("docs-build-badge")) {
            return;
        }

        const badge = document.createElement("aside");
        badge.id = "docs-build-badge";
        badge.className = "docs-build-badge";

        const label = document.createElement("span");
        label.className = "docs-build-badge__label";
        label.textContent = "Docs build";

        const value = document.createElement("span");
        value.className = "docs-build-badge__value";
        value.textContent = builtAt;

        badge.appendChild(label);
        badge.appendChild(value);

        const bottom = document.querySelector(".bottom-of-page");
        if (!bottom) {
            return;
        }

        let rightDetails = bottom.querySelector(".right-details");
        if (!rightDetails) {
            rightDetails = document.createElement("div");
            rightDetails.className = "right-details";
            bottom.appendChild(rightDetails);
        }

        rightDetails.appendChild(badge);
    }

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", createBuildBadge, { once: true });
    } else {
        createBuildBadge();
    }
})();
