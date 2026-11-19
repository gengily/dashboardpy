/* Preview banner. Edit the text below, nothing else needs to change. */
(function () {
  var TEXT = "PREVIEW ONLY. This is a static demo with fake data. Animations, live updates and saving may be missing or behave differently.";
  var s = document.createElement("style");
  s.textContent = "#preview-banner{background:#c62828;color:#fff;font:600 14px/1.35 system-ui,sans-serif;text-align:center;padding:10px 16px;padding-top:calc(10px + env(safe-area-inset-top));position:relative;z-index:50}";
  document.head.appendChild(s);
  var b = document.createElement("div");
  b.id = "preview-banner";
  b.setAttribute("role", "note");
  b.textContent = TEXT;
  document.body.insertBefore(b, document.body.firstChild);
})();
