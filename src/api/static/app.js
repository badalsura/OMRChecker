// Entry point: tabs, API key, capabilities and module wiring.
import { api, loadTemplates, setApiKey, state, toast } from "./api.js";
import { initJobs, onShowJobs } from "./jobs.js";
import { initReview, refreshBadge } from "./review.js";
import { initScan } from "./scan.js";
import { initTemplates } from "./templates.js";

function showTab(name) {
  for (const button of document.querySelectorAll(".tabs button")) {
    button.classList.toggle("active", button.dataset.tab === name);
  }
  for (const section of document.querySelectorAll(".tab")) {
    section.classList.toggle("active", section.id === `tab-${name}`);
  }
  if (name === "jobs") onShowJobs();
  if (name === "review") refreshBadge();
  try {
    history.replaceState(null, "", `#${name}`);
  } catch (e) {
    /* ignore */
  }
}

async function init() {
  for (const button of document.querySelectorAll(".tabs button")) {
    button.addEventListener("click", () => showTab(button.dataset.tab));
  }
  document.getElementById("api-key-btn").addEventListener("click", () => {
    const key = prompt("API key (leave empty if the server has none):", state.apiKey);
    if (key !== null) {
      setApiKey(key.trim());
      boot();
    }
  });
  initScan();
  initReview();
  initJobs();
  initTemplates();
  const initial = (location.hash || "#scan").slice(1);
  if (["scan", "review", "jobs", "templates"].includes(initial)) showTab(initial);
  await boot();
}

async function boot() {
  try {
    state.caps = await api("/capabilities");
    await loadTemplates();
    refreshBadge();
  } catch (error) {
    toast(error.message, "error", 6000);
  }
}

init();
