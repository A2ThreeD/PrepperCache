/*
 * PrepperCache
 * Copyright (c) 2026 Aaron Morris / A2ThreeD.
 * Apply and persist the browser's portal appearance.
 */
(() => {
  const preferenceKey = "preppercache-theme";
  const defaultTheme = "charcoal-blue";
  const validTheme = (theme) => theme === "light" || theme === defaultTheme;

  function applyTheme(theme) {
    document.documentElement.dataset.theme = theme;
    document.querySelectorAll("img[data-blue-src]").forEach((logo) => {
      const blue = theme === defaultTheme;
      logo.width = blue ? 1672 : 2172;
      logo.height = blue ? 941 : 724;
      logo.src = blue ? logo.dataset.blueSrc : logo.dataset.lightSrc;
    });

    const select = document.getElementById("portal-theme");
    if (select) select.value = theme;
  }

  let theme = defaultTheme;
  try {
    const saved = localStorage.getItem(preferenceKey);
    if (validTheme(saved)) theme = saved;
  } catch {
    // The default works when browser storage is unavailable.
  }
  applyTheme(theme);

  document.addEventListener("DOMContentLoaded", () => {
    applyTheme(document.documentElement.dataset.theme);
    const select = document.getElementById("portal-theme");
    select?.addEventListener("change", () => {
      if (!validTheme(select.value)) return;
      applyTheme(select.value);
      try {
        localStorage.setItem(preferenceKey, select.value);
      } catch {
        // Keep the selected appearance for the current page.
      }
    });
  });

  window.addEventListener("storage", (event) => {
    if (event.key === preferenceKey) applyTheme(validTheme(event.newValue) ? event.newValue : defaultTheme);
  });
})();
