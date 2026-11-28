(() => {
  const root = document.documentElement;
  const storageKey = 'pw_theme';
  try {
    const savedTheme = localStorage.getItem(storageKey);
    if (savedTheme === 'dark' || savedTheme === 'light') root.dataset.theme = savedTheme;
  } catch {
    // The current page can still switch themes if storage is unavailable.
  }

  document.addEventListener('DOMContentLoaded', () => {
    const toggle = document.querySelector('[data-theme-toggle]');
    const currentTheme = () => root.dataset.theme || root.dataset.defaultTheme || 'light';
    function updateLabel() {
      const label = currentTheme() === 'dark' ? toggle.dataset.lightLabel : toggle.dataset.darkLabel;
      toggle.setAttribute('aria-label', label);
      toggle.title = label;
    }

    toggle.addEventListener('click', () => {
      const nextTheme = currentTheme() === 'dark' ? 'light' : 'dark';
      root.dataset.theme = nextTheme;
      updateLabel();
      try {
        localStorage.setItem(storageKey, nextTheme);
      } catch {
        // Keep the visible selection for this page.
      }
    });
    updateLabel();
    toggle.hidden = false;
  });
})();
