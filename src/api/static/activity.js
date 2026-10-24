// Who did what: a sheet's history (Results side panel) and the administrators'
// "Check changes" tab: hand sheets out to people for review (split evenly or
// by sheet numbers), change who has what, then check every sheet reviewers
// verified or changed (with its settings and values) and approve it (the
// administrator's second check) or send it back to review.
import { api, chip, el, emit, state, toast } from "./api.js";

const ACTIONS = {
  scanned: "Scanned",
  reviewed: "Reviewed (accepted as read)",
  corrected: "Changed values",
  regraded: "Regraded",
  aligned: "Aligned by hand",
  typed: "Typed the values",
  verified: "Verified",
  approved: "Approved",
  unverified: "Took back the verification",
  undone: "Undid",
  redone: "Redid",
  assigned: "Assigned",
  returned: "Sent back to review",
};
const PAGE = 25;

const when = (at) => (at ? new Date(at * 1000).toLocaleString() : "");
const shown = (v) => (v === "" || v === null || v === undefined ? "∅" : String(v));

export function isAdmin() {
  const user = state.auth && state.auth.user;
  return !user || user.role === "admin";
}

function settingsText(regrade) {
  if (!regrade) return "";
  const all = { ...(regrade.template_overrides || {}), ...(regrade.config_overrides || {}) };
  return Object.keys(all).length ? JSON.stringify(all) : "template settings";
}

function detailText(item) {
  const d = item.detail || {};
  if (item.action === "regraded") {
    const changed = (d.changed || []).length;
    return `${settingsText(d)}${changed ? ` · ${changed} value(s) changed` : ""}`;
  }
  if (item.action === "undone" || item.action === "redone") return `${d.step || "a change"}${d.by ? ` by ${d.by}` : ""}`;
  if (item.action === "aligned") return d.kind === "index" ? `index points ${(d.names || []).join(", ")}` : "page corners";
  if (item.action === "typed") return Object.entries(d.values || {}).map(([k, v]) => `${k}=${shown(v)}`).join(", ");
  if (item.action === "assigned") return `to ${d.to}`;
  if (item.action === "returned") return `${(d.fields || []).join(", ")}${d.note ? ` · “${d.note}”` : ""}`;
  if (d.fields) return d.fields.join(", ") + (d.source === "bulk" ? " (bulk)" : "");
  if (d.accepted && d.accepted.length) return `accepted ${d.accepted.join(", ")}`;
  if (d.job) return d.job;
  return "";
}

// ---------------------------------------------------------------- side panel
export function historyBox(scanId) {
  const list = el("div", { class: "small" }, "Loading…");
  const box = el("details", { class: "res-audit" }, el("summary", {}, "Who did what"), list);
  let loaded = false;
  box.addEventListener("toggle", async () => {
    if (!box.open || loaded) return;
    loaded = true;
    try {
      const data = await api(`/scans/${scanId}/activity`);
      const rows = data.items.slice();
      if (data.scanned && data.scanned.by && !rows.some((i) => i.action === "scanned")) {
        rows.push({ action: "scanned", user: data.scanned.by, at: data.scanned.at });
      }
      list.innerHTML = "";
      if (!rows.length) {
        list.append(el("p", { class: "muted" }, "Nothing recorded for this sheet yet (actions before this log existed are not listed)."));
        return;
      }
      list.append(
        el(
          "ul",
          {},
          rows.map((i) =>
            el("li", {}, el("strong", {}, ACTIONS[i.action] || i.action), ` by ${i.user || "unknown"}`, detailText(i) ? el("span", {}, ` · ${detailText(i)}`) : null, el("span", { class: "muted" }, ` · ${when(i.at)}`))
          )
        )
      );
    } catch (error) {
      list.textContent = error.message;
    }
  });
  return box;
}

// ---------------------------------------------------------------- admin screen
function valuesList(values) {
  if (!values || !values.length) return el("span", { class: "muted" }, "no value changes");
  // The latest change of each value
  const last = {};
  values.forEach((v) => (last[v.name] = v));
  return el(
    "ul",
    { class: "act-values" },
    Object.values(last).map((v) => el("li", {}, el("code", {}, v.name), ` ${shown(v.original ?? v.old)} → ${shown(v.new)}`, el("span", { class: "muted" }, ` · ${v.user}`)))
  );
}

function changeCell(item) {
  const parts = [];
  if (item.regrade) parts.push(el("div", {}, el("strong", {}, "Regraded "), `by ${item.regrade.by}: `, el("code", {}, settingsText(item.regrade))));
  if (item.manual_alignment) parts.push(el("div", {}, el("strong", {}, "Aligned by hand "), `by ${item.manual_alignment.by} (${item.manual_alignment.kind === "index" ? "index points" : "page corners"})`));
  if (item.manual_entry) parts.push(el("div", {}, el("strong", {}, "Values typed "), `by ${item.manual_entry.by}`));
  parts.push(valuesList(item.values));
  return el("td", {}, parts);
}

// ---------------------------------------------------------------- the tab
const $ = (id) => document.getElementById(id);
const tab = { offset: 0, items: [], total: 0, ticked: new Set(), started: false, people: [], picked: new Set(), editing: null };

function filters() {
  const f = { template_id: $("chg-template").value, job_id: $("chg-job").value };
  for (const k of Object.keys(f)) if (!f[k]) delete f[k];
  return f;
}

async function fillJobs() {
  try {
    const data = await api("/jobs?limit=200");
    const select = $("chg-job");
    const current = select.value;
    select.innerHTML = "";
    select.append(el("option", { value: "" }, "All jobs"));
    for (const job of data.jobs) select.append(el("option", { value: job.id }, `${job.name || job.id.slice(0, 8)} (${job.processed_files || 0})`));
    if ([...select.options].some((o) => o.value === current)) select.value = current;
  } catch (e) {
    /* ignore */
  }
}

// People who can be handed work: accounts (sign-in on), else names already used
async function fillPeople() {
  const names = new Set();
  try {
    const data = await api("/auth/users");
    for (const u of data.users || data || []) if (u && u.active !== false && u.name) names.add(u.name);
  } catch (e) {
    /* sign-in off, or not an administrator */
  }
  const accounts = state.auth && state.auth.accounts;
  if (!accounts) {
    try {
      const data = await api(`/assignments/summary?${new URLSearchParams(filters())}`);
      for (const u of data.users) names.add(u.user);
    } catch (e) {
      /* ignore */
    }
    for (const name of tab.picked) names.add(name);
  }
  tab.people = [...names].sort((a, b) => a.localeCompare(b));
  const list = $("chg-people-list");
  list.innerHTML = "";
  for (const name of tab.people) list.append(el("option", { value: name }));
  renderPicker();
  return tab.people;
}

// The people picker: tick who gets work, search to narrow a long list
function renderPicker() {
  const box = $("chg-people");
  const query = $("chg-people-search").value.trim().toLowerCase();
  box.innerHTML = "";
  const shown = tab.people.filter((n) => !query || n.toLowerCase().includes(query));
  if (!tab.people.length) {
    const accounts = state.auth && state.auth.accounts;
    box.append(el("p", { class: "muted small" }, accounts ? "No active accounts yet (Users screen)." : "Type a name above and press Enter."));
  }
  for (const name of shown) {
    const box2 = el("input", { type: "checkbox" });
    box2.checked = tab.picked.has(name);
    box2.addEventListener("change", () => {
      if (box2.checked) tab.picked.add(name);
      else tab.picked.delete(name);
      pickedLine();
    });
    box.append(el("label", { class: "small" }, box2, ` ${name}`));
  }
  if (tab.people.length && !shown.length) box.append(el("p", { class: "muted small" }, "Nobody matches."));
  pickedLine();
}

function pickedLine() {
  const picked = [...tab.picked];
  $("chg-preview").textContent = $("chg-mode").value === "split" ? (picked.length ? `To: ${picked.join(", ")}` : "Tick at least one person") : "";
}

function addTypedPerson() {
  const name = $("chg-people-search").value.trim();
  if (!name) return;
  const accounts = state.auth && state.auth.accounts;
  if (!tab.people.includes(name)) {
    if (accounts) return toast(`No active account named ${name}`, "error");
    tab.people.push(name);
    tab.people.sort((a, b) => a.localeCompare(b));
  }
  tab.picked.add(name);
  $("chg-people-search").value = "";
  renderPicker();
}

function personInput(value = "") {
  return el("input", { class: "chg-person", list: "chg-people-list", placeholder: "person", value, autocomplete: "off" });
}

function addRangeRow(user = "", first = "", last = "") {
  const row = el(
    "div",
    { class: "chg-range-row" },
    personInput(user),
    el("label", { class: "small" }, "sheets ", el("input", { type: "number", min: "1", class: "chg-r-from", placeholder: "from", value: first })),
    el("label", { class: "small" }, "to ", el("input", { type: "number", min: "1", class: "chg-r-to", placeholder: "last", value: last })),
    el("button", { class: "small ghost", title: "Remove this row", onclick: () => row.remove() }, "✕")
  );
  $("chg-range-rows").append(row);
}

function syncMode() {
  const ranges = $("chg-mode").value === "ranges";
  $("chg-split").classList.toggle("hidden", ranges);
  $("chg-ranges").classList.toggle("hidden", !ranges);
  if (ranges && !$("chg-range-rows").children.length) {
    addRangeRow();
    addRangeRow();
  }
  pickedLine();
}

const num = (input) => (Number(input.value) > 0 ? Math.floor(Number(input.value)) : null);
const ago = (at) => {
  if (!at) return "never";
  const s = Date.now() / 1000 - at;
  if (s < 90) return "just now";
  if (s < 5400) return `${Math.round(s / 60)} min ago`;
  if (s < 129600) return `${Math.round(s / 3600)} h ago`;
  return new Date(at * 1000).toLocaleDateString();
};

async function renderSummary() {
  const box = $("chg-summary");
  let data;
  try {
    data = await api(`/assignments/summary?${new URLSearchParams(filters())}`);
  } catch (error) {
    box.textContent = error.message;
    return;
  }
  box.innerHTML = "";
  box.append(el("p", { class: "small" }, `Flagged sheets nobody has yet: ${data.unassigned_flagged}`));
  if (!data.users.length) {
    box.append(el("p", { class: "muted small" }, "No sheets are assigned for this template and job."));
    return;
  }
  const heads = ["Person", "Assigned", "Still to review", "Reviewed", "Verified", "Approved", "Sent back", "Last worked", ""];
  const rows = [];
  for (const u of data.users) {
    rows.push(
      el(
        "tr",
        {},
        el("td", {}, u.user),
        el("td", {}, String(u.assigned)),
        el("td", {}, String(u.to_review)),
        el("td", {}, String(u.reviewed)),
        el("td", {}, String(u.verified)),
        el("td", {}, String(u.approved)),
        el("td", {}, String(u.sent_back)),
        el("td", { class: "small", title: u.last_active ? when(u.last_active) : "" }, ago(u.last_active)),
        el(
          "td",
          {},
          el("button", { class: "small ghost", title: "Open their sheets in Results", onclick: () => emit("results-assigned", { user: u.user, ...filters() }) }, "Their sheets"),
          " ",
          el("button", { class: "small ghost", title: "Show the sheets they changed below", onclick: () => { $("chg-user").value = u.user; tab.offset = 0; refresh(); } }, "Their changes"),
          " ",
          el("button", { class: "small", disabled: !isAdmin(), title: "Move their sheets to someone else, or take them back", onclick: () => { tab.editing = tab.editing === u.user ? null : u.user; renderSummary(); } }, tab.editing === u.user ? "Close" : "Change…")
        )
      )
    );
    if (tab.editing === u.user) rows.push(el("tr", { class: "chg-edit" }, el("td", { colspan: String(heads.length) }, editForm(u))));
  }
  box.append(el("table", { class: "table act-table" }, el("thead", {}, el("tr", {}, heads.map((h) => el("th", {}, h)))), el("tbody", {}, rows)));
}

// Change one person's work: move some or all of it to someone else, or take it back
function editForm(u) {
  const action = el("select", {}, el("option", { value: "move" }, "Move to"), el("option", { value: "clear" }, "Take back (nobody)"));
  const to = personInput();
  const from = el("input", { type: "number", min: "1", placeholder: "first" });
  const till = el("input", { type: "number", min: "1", placeholder: "last" });
  const open = el("input", { type: "checkbox", checked: true });
  action.addEventListener("change", () => to.classList.toggle("hidden", action.value !== "move"));
  const apply = async () => {
    const body = { user: u.user, ...filters(), only_open: open.checked };
    if (num(from)) body.first_sheet = num(from);
    if (num(till)) body.last_sheet = num(till);
    if ((body.first_sheet || body.last_sheet) && !body.job_id) return toast("Pick one job above to give sheet numbers", "error");
    const range = body.first_sheet || body.last_sheet ? ` ${body.first_sheet || "first"} to ${body.last_sheet || "last"} of the` : "";
    const which = open.checked ? "unfinished sheets" : "sheets";
    try {
      if (action.value === "move") {
        body.to = to.value.trim();
        if (!body.to) return toast("Pick who gets the sheets", "error");
        if (!confirm(`Move ${u.user}'s${range} ${which} to ${body.to}?`)) return;
        const r = await api("/assignments/move", { method: "POST", json: body });
        toast(`${r.moved} sheet(s) moved to ${r.to}`, "ok");
      } else {
        if (!confirm(`Take back ${u.user}'s${range} ${which}?`)) return;
        const r = await api("/assignments/clear", { method: "POST", json: body });
        toast(`${r.unassigned} sheet(s) taken back`, "ok");
      }
      tab.editing = null;
      renderSummary();
      fillPeople();
    } catch (error) {
      toast(error.message, "error", 6000);
    }
  };
  return el(
    "div",
    { class: "row gap wrap" },
    el("strong", {}, `${u.user}:`),
    action,
    to,
    el("label", { class: "small", title: "Places in their list, in job order (Their sheets shows the numbers)" }, "their sheets ", from),
    el("label", { class: "small" }, "to ", till),
    el("label", { class: "small", title: "Leave alone the sheets they already reviewed or verified" }, open, " only sheets not finished yet"),
    el("button", { class: "small primary", onclick: apply }, "Apply")
  );
}

async function handOut() {
  const body = { view: $("chg-which").value, only_unassigned: $("chg-only-new").checked, ...filters() };
  if ($("chg-mode").value === "ranges") {
    if (!body.job_id) return toast("Pick one job above: sheet numbers count the sheets of one job", "error", 5000);
    const ranges = [];
    for (const row of $("chg-range-rows").children) {
      const user = row.querySelector(".chg-person").value.trim();
      const first = num(row.querySelector(".chg-r-from"));
      const last = num(row.querySelector(".chg-r-to"));
      if (!user && !first && !last) continue;
      if (!user) return toast("Name a person for every range", "error");
      if (!first && !last) return toast(`Give ${user} a first or last sheet`, "error");
      ranges.push({ user, first_sheet: first, last_sheet: last });
    }
    if (!ranges.length) return toast("Add at least one person and their sheets", "error");
    body.ranges = ranges;
  } else {
    const users = [...tab.picked];
    if (!users.length) return toast("Tick at least one person", "error");
    body.users = users;
    if (num($("chg-from"))) body.first_sheet = num($("chg-from"));
    if (num($("chg-to"))) body.last_sheet = num($("chg-to"));
    if ((body.first_sheet || body.last_sheet) && !body.job_id) return toast("Pick one job above to give sheet numbers", "error");
    if (num($("chg-limit"))) body.limit = num($("chg-limit"));
  }
  try {
    const data = await api("/assignments", { method: "POST", json: body });
    const parts = Object.entries(data.assigned).map(([name, n]) => `${name}: ${n}`);
    toast(data.total ? `Assigned ${data.total} sheet(s) · ${parts.join(", ")}` : "No sheets matched", data.total ? "ok" : "", 5000);
    renderSummary();
  } catch (error) {
    toast(error.message, "error", 6000);
  }
}

async function refresh() {
  const table = $("chg-table");
  const params = new URLSearchParams({ limit: PAGE, offset: tab.offset, ...filters() });
  if ($("chg-user").value.trim()) params.set("user", $("chg-user").value.trim());
  if ($("chg-all").checked) params.set("include_checked", "true");
  try {
    const data = await api(`/activity/sheets?${params}`);
    tab.items = data.items;
    tab.total = data.total;
  } catch (error) {
    table.textContent = error.message;
    return;
  }
  tab.ticked.clear();
  syncButtons();
  const end = Math.min(tab.offset + tab.items.length, tab.total);
  $("chg-count").textContent = tab.total ? `${tab.offset + 1}–${end} of ${tab.total} sheet(s)${$("chg-all").checked ? "" : " waiting for approval"}` : "";
  $("chg-prev").disabled = tab.offset === 0;
  $("chg-next").disabled = end >= tab.total;
  table.innerHTML = "";
  if (!tab.items.length) {
    table.append(el("p", { class: "muted" }, $("chg-all").checked ? "No sheet was verified or changed after scanning." : "Every verified or changed sheet is approved."));
    return;
  }
  const head = el("input", { type: "checkbox", title: "Tick every sheet on this page" });
  const boxes = [];
  head.addEventListener("change", () => {
    boxes.forEach(([box, id]) => {
      box.checked = head.checked;
      if (head.checked) tab.ticked.add(id);
      else tab.ticked.delete(id);
    });
    syncButtons();
  });
  table.append(
    el(
      "table",
      { class: "table act-table" },
      el("thead", {}, el("tr", {}, el("th", {}, head), el("th", {}, "Sheet"), el("th", {}, "Who"), el("th", {}, "Changed settings and values"), el("th", {}, ""))),
      el(
        "tbody",
        {},
        tab.items.map((item) => {
          const box = el("input", { type: "checkbox" });
          boxes.push([box, item.scan_id]);
          box.addEventListener("change", () => {
            if (box.checked) tab.ticked.add(item.scan_id);
            else tab.ticked.delete(item.scan_id);
            syncButtons();
          });
          const verified = item.verified ? el("div", { class: "small" }, chip(`verified by ${item.verified.by}`, "ok")) : el("div", { class: "small" }, chip("not verified"));
          const approved = item.approved ? el("div", { class: "small" }, chip(`approved by ${item.approved.by}`, "ok")) : null;
          return el(
            "tr",
            {},
            el("td", {}, box),
            el("td", {}, el("div", { class: "mono" }, item.file_name || item.scan_id), el("div", {}, chip(item.status, item.status === "ok" ? "ok" : "flag")), verified, approved, el("div", { class: "muted small" }, when(item.changed_at))),
            el(
              "td",
              { class: "small" },
              item.scanned && item.scanned.by ? el("div", {}, `Scanned: ${item.scanned.by}`) : null,
              item.assigned_to ? el("div", {}, `Assigned to: ${item.assigned_to.user}`) : null,
              el("div", {}, `Changed: ${item.users.join(", ") || "unknown"}`),
              el("div", { class: "muted" }, item.actions.map((a) => ACTIONS[a] || a).join(", "))
            ),
            changeCell(item),
            el(
              "td",
              {},
              el("button", { class: "small", title: "Open this sheet in Results", onclick: () => emit("results-scan", item.scan_id) }, "Open"),
              " ",
              el("button", { class: "small primary", disabled: !isAdmin() || !!item.approved, title: item.verified ? "Approve: your second check of the reviewer's work" : "Approve (this also verifies the sheet: nobody verified it yet)", onclick: () => approve([item.scan_id]) }, item.approved ? "Approved" : "Approve"),
              " ",
              el("button", { class: "small ghost", disabled: !isAdmin(), title: "Take back the verification and put its flagged and changed values back in the review queue", onclick: () => sendBack([item.scan_id]) }, "Send back")
            )
          );
        })
      )
    )
  );
}

function syncButtons() {
  const n = tab.ticked.size;
  $("chg-verify").textContent = n ? `Approve ticked (${n})` : "Approve ticked";
  $("chg-return").textContent = n ? `Send ticked back (${n})` : "Send ticked back";
  $("chg-verify").disabled = $("chg-return").disabled = !n || !isAdmin();
  $("chg-verify-page").disabled = !isAdmin();
}

async function approve(ids) {
  if (!ids.length) return;
  const unverified = tab.items.filter((i) => ids.includes(i.scan_id) && !i.verified).length;
  const also = unverified ? ` ${unverified} of them nobody verified yet: they are verified too, and their flagged values accepted as they are now.` : "";
  if ((ids.length > 1 || unverified) && !confirm(`Approve ${ids.length} sheet(s) with their current values?${also}`)) return;
  try {
    const data = await api("/activity/approve", { method: "POST", json: { scan_ids: ids } });
    const failed = data.failed.length ? ` · ${data.failed.length} not approved: ${data.failed.map((f) => f.error).join("; ")}` : "";
    toast(`${data.approved.length} sheet(s) approved${failed}`, data.failed.length ? "error" : "ok", failed ? 7000 : 3000);
    emit("review-changed");
    emit("results-deleted");
  } catch (error) {
    toast(error.message, "error", 6000);
  }
  refresh();
  renderSummary();
}

async function sendBack(ids) {
  if (!ids.length) return;
  const to = prompt(`Send ${ids.length} sheet(s) back to review. Hand them to (a name, or leave empty to keep the current assignee)${tab.people.length ? `\nPeople: ${tab.people.join(", ")}` : ""}:`, "");
  if (to === null) return;
  const note = prompt("A note for the reviewer (optional):", "") || "";
  try {
    const data = await api("/activity/return", { method: "POST", json: { scan_ids: ids, assign_to: to.trim() || null, note: note.trim() || null } });
    const failed = data.failed.length ? ` · ${data.failed.length} not sent: ${data.failed.map((f) => f.error).join("; ")}` : "";
    toast(`${data.returned.length} sheet(s) back in the review queue${failed}`, data.failed.length ? "error" : "ok", failed ? 7000 : 3000);
    emit("review-changed");
    emit("results-deleted");
  } catch (error) {
    toast(error.message, "error", 6000);
  }
  refresh();
  renderSummary();
}

export function initChanges() {
  const reload = () => {
    tab.offset = 0;
    refresh();
    renderSummary();
    fillPeople();
  };
  $("chg-template").addEventListener("change", reload);
  $("chg-job").addEventListener("change", reload);
  $("chg-all").addEventListener("change", reload);
  let timer = null;
  $("chg-user").addEventListener("input", () => {
    clearTimeout(timer);
    timer = setTimeout(reload, 300);
  });
  $("chg-prev").addEventListener("click", () => {
    tab.offset = Math.max(0, tab.offset - PAGE);
    refresh();
  });
  $("chg-next").addEventListener("click", () => {
    tab.offset += PAGE;
    refresh();
  });
  $("chg-assign").addEventListener("click", handOut);
  $("chg-mode").addEventListener("change", syncMode);
  $("chg-range-add").addEventListener("click", () => addRangeRow());
  $("chg-people-search").addEventListener("input", renderPicker);
  $("chg-people-search").addEventListener("keydown", (event) => {
    if (event.key === "Enter") {
      event.preventDefault();
      addTypedPerson();
    }
  });
  $("chg-verify").addEventListener("click", () => approve([...tab.ticked]));
  $("chg-return").addEventListener("click", () => sendBack([...tab.ticked]));
  $("chg-verify-page").addEventListener("click", () => approve(tab.items.filter((i) => !i.approved).map((i) => i.scan_id)));
}

// The tab is for administrators when sign-in is on
export function syncChangesTab() {
  const button = document.querySelector('.tabs button[data-tab="changes"]');
  if (button) button.classList.toggle("hidden", !isAdmin());
}

export function onShowChanges() {
  if (!tab.started) {
    tab.started = true;
    fillJobs();
  }
  fillPeople();
  refresh();
  renderSummary();
}
