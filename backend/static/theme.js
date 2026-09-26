/* The only browser preference persisted is the user's visual theme. */
(() => {
  "use strict";
  let preference = "system";
  try { preference = localStorage.getItem("relay-theme") || "system"; } catch (_) {}
  if (!["light", "dark", "system"].includes(preference)) preference = "system";
  const media = matchMedia("(prefers-color-scheme: dark)");
  const apply = () => {
    const effective = preference === "system" ? (media.matches ? "dark" : "light") : preference;
    document.documentElement.dataset.theme = effective;
    const button = document.getElementById("theme-toggle");
    if (button) {
      const names = {system: "跟随系统", light: "浅色", dark: "深色"};
      button.textContent = names[preference];
      button.setAttribute("aria-label", `外观：${names[preference]}。点击切换`);
    }
  };
  apply();
  media.addEventListener("change", apply);
  addEventListener("DOMContentLoaded", () => {
    apply();
    document.getElementById("theme-toggle").addEventListener("click", () => {
      preference = {system: "light", light: "dark", dark: "system"}[preference];
      try { localStorage.setItem("relay-theme", preference); } catch (_) {}
      apply();
    });
  });
})();
