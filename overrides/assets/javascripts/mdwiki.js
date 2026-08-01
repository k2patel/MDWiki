// MDWiki interactions and legacy fragment compatibility.
document.addEventListener("click", (event) => {
  const trigger = event.target.closest("[data-wiki-search]");
  if (!trigger) return;

  const searchToggle = document.getElementById("__search");
  if (!searchToggle) return;

  searchToggle.checked = true;
  searchToggle.dispatchEvent(new Event("change", { bubbles: true }));
  window.requestAnimationFrame(() => {
    document.querySelector(".md-search__input")?.focus();
  });
});

function normalizeLegacyFragment() {
  if (!window.location.hash) return;
  const legacy = decodeURIComponent(window.location.hash.slice(1));
  if (document.getElementById(legacy)) return;

  const canonical = legacy
    .toLowerCase()
    .replaceAll("_", "-")
    .replace(/[^a-z0-9-]+/g, "-")
    .replace(/-+/g, "-")
    .replace(/^-|-$/g, "");
  const target = document.getElementById(canonical);
  if (!target) return;

  window.history.replaceState(null, "", `${window.location.pathname}${window.location.search}#${canonical}`);
  target.scrollIntoView();
}

window.addEventListener("hashchange", normalizeLegacyFragment);
if (typeof document$ !== "undefined") {
  document$.subscribe(normalizeLegacyFragment);
} else {
  document.addEventListener("DOMContentLoaded", normalizeLegacyFragment);
}
