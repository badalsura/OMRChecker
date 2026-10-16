// Entry point: tabs, API key, capabilities and module wiring.
import { api, loadTemplates, setApiKey, setUser, state, toast } from "./api.js";
import { initAuth, loadAuth, loginRequired, openAccountMenu, showLogin } from "./auth.js";
import { initChanges, onShowChanges, syncChangesTab } from "./activity.js";
import { initDesktop, isDesktop } from "./desktop.js";
import { initExport, refreshExports } from "./export.js";
import { initJobs, onShowJobs } from "./jobs.js";
import { bindToolbar, initResults, onShowResults } from "./results.js";
import { initReview, refreshBadge } from "./review.js";
import { initScan } from "./scan.js";
import { initTemplates } from "./templates.js";

const TABS = ["scan", "review", "results", "changes", "jobs", "templates"];

function showTab(name) {
  for (const button of document.querySelectorAll(".tabs button")) {
    button.classList.toggle("active", button.dataset.tab === name);
  }
  for (const section of document.querySelectorAll(".tab")) {
    section.classList.toggle("active", section.id === `tab-${name}`);
  }
  if (name === "jobs") {
    onShowJobs();
    refreshExports();
  }
  if (name === "review") refreshBadge();
  if (name === "results") onShowResults();
  if (name === "changes") onShowChanges();
  try {
    history.replaceState(null, "", `#${name}`);
  } catch (e) {
    /* ignore */
  }
}

function showUser() {
  syncChangesTab();
  const signedIn = state.auth && state.auth.user;
  document.getElementById("user-btn").textContent = signedIn ? `👤 ${signedIn.display || signedIn.name}` : state.user ? `User: ${state.user}` : "User";
}

// Product name from branding/branding.json: tab title and every .brand-name
async function applyBranding() {
  try {
    const response = await fetch("/branding");
    if (!response.ok) return;
    const brand = await response.json();
    if (!brand.name) return;
    document.title = brand.name;
    for (const node of document.querySelectorAll(".brand-name")) node.textContent = brand.name;
  } catch (error) {
    // The name already in the page stays
  }
}

async function init() {
  if (isDesktop()) initDesktop();
  applyBranding();
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
  document.getElementById("user-btn").addEventListener("click", () =>
    openAccountMenu((name) => {
      setUser(name);
      showUser();
    })
  );
  initAuth(async () => {
    showUser();
    await boot();
  });
  await loadAuth();
  // Every window of the app (also one opened from Help) behaves as the app
  if (state.auth.desktop && !isDesktop()) initDesktop();
  showUser();
  initScan();
  initReview();
  initResults();
  bindToolbar();
  initChanges();
  syncChangesTab();
  initExport();
  initJobs();
  initTemplates();
  const initial = (location.hash || "#scan").slice(1);
  if (TABS.includes(initial)) showTab(initial);
  // With accounts on, nothing loads until someone signs in
  if (loginRequired()) return showLogin();
  await boot();
}

async function boot() {
  try {
    state.caps = await api("/capabilities");
    // Show what "auto" means for the job worker count on this machine
    const workers = document.getElementById("job-workers");
    if (workers && state.caps.workers) workers.placeholder = `auto (${state.caps.workers})`;
    await loadTemplates();
    refreshBadge();
  } catch (error) {
    toast(error.message, "error", 6000);
  }
}

init();
