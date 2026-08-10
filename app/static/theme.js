(() => {
  const themeStorageKey = "xuying-theme";
  const accentStorageKey = "xuying-accent";
  const systemTheme = window.matchMedia("(prefers-color-scheme: dark)");
  const validThemes = new Set(["auto", "light", "dark"]);
  const validAccents = new Set(["mint", "cyan", "blue", "amber"]);
  const isStandalone = window.matchMedia("(display-mode: standalone)").matches
    || window.navigator.standalone === true;
  document.documentElement.classList.toggle("is-standalone", isStandalone);

  function readSetting(key, fallback, validValues) {
    try {
      const value = localStorage.getItem(key) || fallback;
      return validValues.has(value) ? value : fallback;
    } catch (_error) {
      return fallback;
    }
  }

  function saveSetting(key, value) {
    try {
      localStorage.setItem(key, value);
    } catch (_error) {
      // Some private browsing modes block storage. The current page still updates.
    }
  }

  function selectedTheme() {
    return readSetting(themeStorageKey, "auto", validThemes);
  }

  function selectedAccent() {
    return readSetting(accentStorageKey, "mint", validAccents);
  }

  function resolvedTheme(mode) {
    return mode === "auto" ? (systemTheme.matches ? "dark" : "light") : mode;
  }

  function themedIconUrl(resolved, size = 192) {
    const variant = resolved === "dark" ? "dark" : "light";
    return `/static/icons/icon-xuying-${variant}-${size}.png?v=45`;
  }

  function updateThemeIcons(resolved) {
    const variant = resolved === "dark" ? "dark" : "light";
    const touchIcon = document.getElementById("apple-touch-icon");
    if (touchIcon) {
      touchIcon.href = `/static/icons/apple-touch-icon-${variant}-v2.png?v=45`;
    }
    document.querySelectorAll("[data-theme-app-icon]").forEach((image) => {
      image.src = themedIconUrl(resolved);
    });
  }

  function refreshSelectedButtons() {
    const theme = selectedTheme();
    const accent = selectedAccent();
    document.querySelectorAll("[data-theme-choice]").forEach((button) => {
      const active = button.dataset.themeChoice === theme;
      button.classList.toggle("active", active);
      button.setAttribute("aria-pressed", String(active));
    });
    document.querySelectorAll("[data-accent-choice]").forEach((button) => {
      const active = button.dataset.accentChoice === accent;
      button.classList.toggle("active", active);
      button.setAttribute("aria-pressed", String(active));
    });
  }

  function applyTheme(mode, save = false) {
    const safeMode = validThemes.has(mode) ? mode : "auto";
    const resolved = resolvedTheme(safeMode);
    document.documentElement.dataset.themeMode = safeMode;
    document.documentElement.dataset.theme = resolved;
    document.documentElement.style.colorScheme = resolved;
    updateThemeIcons(resolved);
    if (save) saveSetting(themeStorageKey, safeMode);
    refreshSelectedButtons();
  }

  function applyAccent(accent, save = false) {
    const safeAccent = validAccents.has(accent) ? accent : "mint";
    document.documentElement.dataset.accent = safeAccent;
    if (save) saveSetting(accentStorageKey, safeAccent);
    refreshSelectedButtons();
  }

  applyTheme(selectedTheme());
  applyAccent(selectedAccent());
  systemTheme.addEventListener("change", () => {
    if (selectedTheme() === "auto") applyTheme("auto");
  });

  const icons = {
    home: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M3.5 11 12 4l8.5 7v8.5H15v-6H9v6H3.5z"/></svg>',
    telegram: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M21 4 3.5 10.8c-.8.3-.8 1.4.1 1.7l4.4 1.4 1.7 5c.3.8 1.3 1 1.8.3l2.4-2.8 4.2 3.1c.7.5 1.7.1 1.9-.8z"/><path d="m8 13.9 9-6.4-7.3 8.4"/></svg>',
    history: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4.2 8.5A8 8 0 1 1 4 15"/><path d="M4 5v5h5M12 8v5l3.5 2"/></svg>',
    immich: '<svg class="immich-mark" viewBox="0 0 24 24" aria-hidden="true"><ellipse cx="12" cy="6.1" rx="2.4" ry="4.1"/><ellipse cx="17.1" cy="9" rx="2.4" ry="4.1" transform="rotate(60 17.1 9)"/><ellipse cx="17.1" cy="15" rx="2.4" ry="4.1" transform="rotate(120 17.1 15)"/><ellipse cx="12" cy="17.9" rx="2.4" ry="4.1"/><ellipse cx="6.9" cy="15" rx="2.4" ry="4.1" transform="rotate(60 6.9 15)"/><ellipse cx="6.9" cy="9" rx="2.4" ry="4.1" transform="rotate(120 6.9 9)"/><circle cx="12" cy="12" r="2.2"/></svg>',
    sun: '<svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/></svg>',
    auto: '<svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="8"/><path d="M12 4v16"/></svg>',
    moon: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M19.4 15.2A8 8 0 0 1 8.8 4.6 8 8 0 1 0 19.4 15.2z"/></svg>',
  };

  const navItems = [
    { href: "/", key: "home", label: "总览" },
    { href: "/setup", key: "telegram", label: "频道" },
    { href: "/rebuild", key: "history", label: "补全" },
    { href: "/immich", key: "immich", label: "Immich" },
  ];

  const accentItems = [
    { key: "mint", label: "极光青绿" },
    { key: "cyan", label: "冰川青蓝" },
    { key: "blue", label: "星际蓝" },
    { key: "amber", label: "琥珀金" },
  ];

  function normalizedPath() {
    return window.location.pathname.replace(/\/+$/, "") || "/";
  }

  function buildChrome() {
    document.body.classList.add("has-modern-shell");
    const path = normalizedPath();
    const topbar = document.createElement("header");
    topbar.className = "app-topbar";
    topbar.innerHTML = `
      <a class="app-brand" href="/" aria-label="返回序影总览">
        <span class="app-logo"><img data-theme-app-icon src="${themedIconUrl(document.documentElement.dataset.theme)}" alt=""></span>
        <span><strong>序影</strong><small>Telegram → Immich</small></span>
      </a>
      <div class="topbar-actions">
        <span class="local-chip"><i></i>本地服务</span>
        <div class="accent-switch" aria-label="主题颜色">
          ${accentItems.map((item) => `<button type="button" data-accent-choice="${item.key}" title="${item.label}" aria-label="${item.label}"><i></i></button>`).join("")}
        </div>
        <div class="theme-switch" aria-label="明暗模式">
          <button type="button" data-theme-choice="light" title="浅色模式" aria-label="浅色模式">${icons.sun}</button>
          <button type="button" data-theme-choice="auto" title="跟随系统" aria-label="跟随系统">${icons.auto}</button>
          <button type="button" data-theme-choice="dark" title="深色模式" aria-label="深色模式">${icons.moon}</button>
        </div>
      </div>`;

    const nav = document.createElement("nav");
    nav.className = "app-bottom-nav";
    nav.setAttribute("aria-label", "主导航");
    nav.innerHTML = navItems.map((item) => `
      <a href="${item.href}" class="${path === item.href ? "active" : ""}">
        ${icons[item.key]}<span>${item.label}</span>
      </a>`).join("");

    document.body.prepend(topbar);
    document.body.append(nav);

    topbar.querySelectorAll("[data-theme-choice]").forEach((button) => {
      button.addEventListener("click", () => applyTheme(button.dataset.themeChoice, true));
    });
    topbar.querySelectorAll("[data-accent-choice]").forEach((button) => {
      button.addEventListener("click", () => applyAccent(button.dataset.accentChoice, true));
    });
    refreshSelectedButtons();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", buildChrome, { once: true });
  } else {
    buildChrome();
  }

  if ("serviceWorker" in navigator
      && (window.location.protocol === "https:"
        || ["localhost", "127.0.0.1"].includes(window.location.hostname))) {
    window.addEventListener("load", () => {
      navigator.serviceWorker.register("/service-worker.js", { scope: "/" }).catch(() => {
        // The application still works normally when service workers are unavailable.
      });
    }, { once: true });
  }
})();
