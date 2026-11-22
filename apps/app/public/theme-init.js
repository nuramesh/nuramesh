// Applies the saved theme before the first paint so the page never flashes the wrong colours.
// Loaded synchronously from <head>; keep it tiny and in sync with src/lib/theme.ts (same key/values).
(function () {
  var preference = "system";
  try {
    var stored = window.localStorage.getItem("nuramesh-theme");
    if (stored === "light" || stored === "dark" || stored === "system") preference = stored;
  } catch {
    // Storage can be blocked (privacy mode); fall back to the system setting.
  }
  var dark =
    preference === "dark" ||
    (preference === "system" && window.matchMedia("(prefers-color-scheme: dark)").matches);
  document.documentElement.classList.toggle("dark", dark);
  document.documentElement.dataset.themePreference = preference;
})();
