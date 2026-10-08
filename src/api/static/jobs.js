// Jobs tab: start bulk jobs (chunked uploads or a server folder) and watch progress.
import { api, chip, chunk, el, emit, fmtDuration, modal, state, toast, url } from "./api.js";
import { includeSubfolders, initFolderPicker, onShowFolders } from "./folders.js";
import { appendPdfParams, mountPdfOptions } from "./pdf_options.js";

const UPLOAD_CHUNK = 25;
let pollTimer = null;

export function initJobs() {
  mountPdfOptions("job-pdf", "job");
  document.getElementById("job-start").addEventListener("click", startJob);
  document.getElementById("jobs-refresh").addEventListener("click", refreshJobs);
  document.getElementById("job-dir-mode").addEventListener("change", (e) => {
    const input = document.getElementById("job-files");
    if (e.target.checked) input.setAttribute("webkitdirectory", "");
    else input.removeAttribute("webkitdirectory");
  });
  initFolderPicker();
}

export function onShowJobs() {
  onShowFolders();
  refreshJobs();
}

const ACCEPTED = /\.(png|jpe?g|tiff?|bmp|webp|pdf)$/i;

async function startJob() {
  const templateId = document.getElementById("job-template").value;
  if (!templateId) return toast("Pick a template", "error");
  const files = [...document.getElementById("job-files").files].filter((f) => ACCEPTED.test(f.name));
  const folder = document.getElementById("job-folder").value.trim();
  if (!files.length && !folder) return toast("Choose files or enter a server folder", "error");
  const status = document.getElementById("job-upload-status");
  const button = document.getElementById("job-start");
  const base = () => {
    const form = new FormData();
    form.append("template_id", templateId);
    form.append("save_images", document.getElementById("job-images").value);
    appendPdfParams(form, "job");
    const name = document.getElementById("job-name").value.trim();
    if (name) form.append("name", name);
    const workers = document.getElementById("job-workers").value;
    if (workers) form.append("workers", workers);
    const prefetch = document.getElementById("job-prefetch").value;
    if (prefetch !== "") form.append("prefetch", prefetch);
    return form;
  };
  button.disabled = true;
  try {
    let job;
    if (folder && !files.length) {
      const form = base();
      form.append("folder", folder);
      form.append("recursive", includeSubfolders() ? "true" : "false");
      job = await api("/jobs", { method: "POST", form });
    } else {
      const parts = chunk(files, UPLOAD_CHUNK);
      const form = base();
      form.append("start", "false");
      if (folder) {
        form.append("folder", folder);
        form.append("recursive", includeSubfolders() ? "true" : "false");
      }
      for (const f of parts[0]) form.append("files", f, f.webkitRelativePath ? f.webkitRelativePath.replace(/\//g, "_") : f.name);
      job = await api("/jobs", { method: "POST", form });
      let sent = parts[0].length;
      status.textContent = `Uploading ${sent}/${files.length}…`;
      for (const part of parts.slice(1)) {
        const more = new FormData();
        for (const f of part) more.append("files", f, f.webkitRelativePath ? f.webkitRelativePath.replace(/\//g, "_") : f.name);
        await api(`/jobs/${job.id}/files`, { method: "POST", form: more });
        sent += part.length;
        status.textContent = `Uploading ${sent}/${files.length}…`;
      }
      job = await api(`/jobs/${job.id}/start`, { method: "POST" });
    }
    status.textContent = `Job ${job.id.slice(0, 8)} started with ${job.total_files} files`;
    document.getElementById("job-files").value = "";
    toast("Job started", "ok");
    for (const warning of job.warnings || []) toast(warning, "warn", 8000);
    onShowFolders();
    refreshJobs();
  } catch (error) {
    toast(error.message, "error", 6000);
    status.textContent = "";
  } finally {
    button.disabled = false;
  }
}

export async function refreshJobs() {
  clearTimeout(pollTimer);
  let data;
  try {
    data = await api("/jobs?limit=50");
  } catch (error) {
    toast(error.message, "error");
    return;
  }
  const names = Object.fromEntries(state.templates.map((t) => [t.id, t.name]));
  const body = document.querySelector("#jobs-table tbody");
  body.innerHTML = "";
  let active = false;
  for (const job of data.jobs) {
    if (["queued", "running", "uploading"].includes(job.state)) active = true;
    const total = job.total_files || 0;
    const pct = total ? Math.round((100 * (job.processed_files || 0)) / total) : 0;
    const counts = job.counts || {};
    body.append(
      el(
        "tr",
        {},
        el("td", {}, el("div", {}, job.name || job.id.slice(0, 8)), el("div", { class: "muted small" }, job.source === "folder" ? job.folder || "folder" : "upload")),
        el("td", {}, names[job.template_id] || job.template_id),
        el("td", {}, chip(job.state, job.state)),
        el("td", {}, el("div", { class: "progress" }, el("div", { style: { width: `${pct}%` } })), el("div", { class: "muted small" }, `${job.processed_files || 0} / ${total} files · ${pct}%`)),
        el("td", {}, counts.ok || 0),
        el("td", {}, counts.needs_review || 0),
        el("td", {}, counts.error || 0),
        el("td", {}, job.throughput_per_s ?? ""),
        el("td", {}, job.state === "running" ? fmtDuration(job.eta_s) : ""),
        el(
          "td",
          { class: "actions" },
          el("a", { class: "button small", href: url(`/jobs/${job.id}/results.csv`) }, "CSV"),
          el("button", { class: "small", onclick: () => emit("export-open", { job_id: job.id, template_id: job.template_id }) }, "Export…"),
          job.pages ? el("button", { class: "small", onclick: () => emit("results-job", job.id) }, "Results") : null,
          counts.needs_review ? el("button", { class: "small", onclick: () => emit("review-job", job.id) }, "Review") : null,
          ["queued", "running"].includes(job.state)
            ? el("button", { class: "small", title: "Finish the sheets being read, then stop; Resume reads the rest", onclick: () => jobAction(job, "pause") }, "Pause")
            : null,
          ["paused", "interrupted"].includes(job.state)
            ? el("button", { class: "small primary", title: "Read the remaining sheets", onclick: () => jobAction(job, "resume") }, "Resume")
            : null,
          ["queued", "running", "uploading", "paused", "interrupted"].includes(job.state)
            ? el("button", { class: "small danger", onclick: () => cancelJob(job) }, "Cancel")
            : el("button", { class: "small danger ghost", title: "Delete this job and its results", onclick: () => deleteJob(job) }, "Delete")
        )
      )
    );
  }
  if (!data.jobs.length) body.append(el("tr", {}, el("td", { colspan: 10, class: "muted" }, "No jobs yet")));
  if (active && document.getElementById("tab-jobs").classList.contains("active")) {
    pollTimer = setTimeout(refreshJobs, 1500);
  }
  if (!active) emit("review-changed");
}

// Delete a job and every result it produced, after a confirmation
async function jobAction(job, action) {
  try {
    await api(`/jobs/${job.id}/${action}`, { method: "POST" });
    toast(action === "pause" ? "Pausing: sheets being read finish first" : "Job resumed", "ok");
  } catch (error) {
    toast(error.message, "error", 6000);
  }
  refreshJobs();
}

function cancelJob(job) {
  const label = job.name || job.id.slice(0, 8);
  const done = job.processed_files || 0;
  const go = el(
    "button",
    {
      class: "danger",
      onclick: async () => {
        go.disabled = true;
        try {
          await api(`/jobs/${job.id}/cancel`, { method: "POST" });
          dialog.close();
          toast(`Job ${label} cancelled`, "ok");
        } catch (error) {
          go.disabled = false;
          toast(error.message, "error", 6000);
        }
        refreshJobs();
      },
    },
    "Cancel job"
  );
  const dialog = modal(
    `Cancel job ${label}?`,
    el(
      "div",
      {},
      el("p", {}, `The remaining sheets will not be read. The ${done} sheet(s) already read keep their results.`),
      el("p", { class: "small muted" }, "A cancelled job cannot be resumed. To stop for now and continue later, use Pause instead.")
    ),
    [go]
  );
}

function deleteJob(job) {
  const pages = job.pages || 0;
  const label = job.name || job.id.slice(0, 8);
  const typed = el("input", { placeholder: "delete", size: 10, autocomplete: "off" });
  const go = el(
    "button",
    {
      class: "danger",
      onclick: async () => {
        if (typed.value.trim().toLowerCase() !== "delete") {
          typed.classList.add("invalid");
          typed.focus();
          return;
        }
        go.disabled = true;
        try {
          const done = await api(`/jobs/${job.id}`, { method: "DELETE" });
          dialog.close();
          toast(`Job ${label} deleted with ${done.scans} sheet result(s)`, "ok");
          emit("review-changed");
          emit("results-deleted", { job_id: job.id });
          refreshJobs();
        } catch (error) {
          go.disabled = false;
          toast(error.message, "error", 6000);
        }
      },
    },
    "Delete job"
  );
  const dialog = modal(
    `Delete job ${label}?`,
    el(
      "div",
      {},
      el("p", {}, `This deletes the job and its ${pages} sheet result(s): values, corrections, review items and stored images. It cannot be undone.`),
      el("p", { class: "small muted" }, job.source === "folder" ? `The scans in ${job.folder || "the server folder"} are not touched.` : "The uploaded copies of the scans are deleted too."),
      el("p", { class: "small muted" }, "The deletion is written to the audit log with your name."),
      el("label", { class: "small" }, "Type ", el("code", {}, "delete"), " to confirm: ", typed)
    ),
    [go]
  );
  typed.focus();
}
