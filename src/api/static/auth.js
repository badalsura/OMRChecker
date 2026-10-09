// Login, registration and account management for the station GUI.
// Login is off until the first account (the administrator) is created; from
// then on the GUI asks everyone to sign in. Sessions are an HttpOnly cookie,
// so images, downloads and the live camera page are signed in too.
import { api, el, modal, state, toast } from "./api.js";

let overlay = null;
let onSignedIn = null;

export async function loadAuth() {
  try {
    state.auth = await api("/auth/status", { retry: false });
  } catch (error) {
    state.auth = { accounts: false, user: null, registration: "first" };
  }
  return state.auth;
}

export const signedIn = () => !!(state.auth && state.auth.user);
export const loginRequired = () => !!(state.auth && state.auth.accounts && !state.auth.user);

// Full-page sign-in / registration form; resolves once signed in
export function showLogin(message) {
  if (overlay) return;
  const auth = state.auth || {};
  const first = !auth.accounts;
  let mode = first ? "register" : "login";
  const name = el("input", { autocomplete: "username", placeholder: "user name", required: true });
  const display = el("input", { autocomplete: "name", placeholder: "optional" });
  const password = el("input", { type: "password", autocomplete: "current-password", required: true });
  const repeat = el("input", { type: "password", autocomplete: "new-password" });
  const note = el("div", { class: "auth-note" }, message || "");
  const submit = el("button", { class: "primary", type: "submit" });
  const switcher = el("button", { class: "ghost small", type: "button" });
  const displayRow = el("label", { class: "field" }, "Full name", display);
  const repeatRow = el("label", { class: "field" }, "Password again", repeat);
  const title = el("h2", {});
  const render = () => {
    const registering = mode === "register";
    title.textContent = first ? "Create the administrator account" : registering ? "Create an account" : "Sign in";
    submit.textContent = registering ? "Create account" : "Sign in";
    password.autocomplete = registering ? "new-password" : "current-password";
    // .field sets display, which beats the hidden attribute
    displayRow.style.display = repeatRow.style.display = registering ? "" : "none";
    switcher.style.display = first || auth.registration === "closed" ? "none" : "";
    switcher.textContent = registering ? "I have an account: sign in" : "New here? Create an account";
  };
  switcher.addEventListener("click", () => {
    mode = mode === "login" ? "register" : "login";
    note.textContent = "";
    render();
  });
  const form = el(
    "form",
    { class: "auth-card" },
    el("div", { class: "brand" }, el("span", { class: "dot" }), "OMR Station"),
    title,
    first ? el("p", { class: "muted small" }, "From now on everyone signs in to use this station. Programs and the SDK keep using the API key.") : null,
    el("label", { class: "field" }, "User name", name),
    displayRow,
    el("label", { class: "field" }, "Password", password),
    repeatRow,
    note,
    el("div", { class: "row gap" }, submit, switcher),
    first ? el("button", { class: "ghost small", type: "button", onclick: () => hideLogin() }, "Not now") : null
  );
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    note.textContent = "";
    note.className = "auth-note";
    try {
      if (mode === "register") {
        if (password.value !== repeat.value) throw new Error("The two passwords differ");
        const done = await api("/auth/register", { method: "POST", json: { username: name.value.trim(), password: password.value, display: display.value.trim() || null }, retry: false });
        if (!done.signed_in) {
          note.textContent = "Account created. An administrator has to approve it before you can sign in.";
          note.className = "auth-note ok";
          mode = "login";
          password.value = repeat.value = "";
          render();
          return;
        }
      } else {
        await api("/auth/login", { method: "POST", json: { username: name.value.trim(), password: password.value }, retry: false });
      }
      await loadAuth();
      hideLogin();
      if (onSignedIn) onSignedIn();
    } catch (error) {
      note.textContent = error.message.replace(/^\d+: /, "");
      note.className = "auth-note err";
    }
  });
  render();
  overlay = el("div", { class: "auth-overlay" }, form);
  document.body.append(overlay);
  setTimeout(() => name.focus(), 0);
}

function hideLogin() {
  if (overlay) overlay.remove();
  overlay = null;
}

export function initAuth(afterSignIn) {
  onSignedIn = afterSignIn;
  // A 401 from any call means the session ended: sign in again
  state.onUnauthorized = () => {
    if (state.auth && state.auth.accounts) {
      state.auth.user = null;
      showLogin("Your session ended. Sign in again.");
      return true;
    }
    return false;
  };
}

// The topbar user button: account menu when login is on
export function openAccountMenu(showUser) {
  const auth = state.auth || {};
  if (!auth.accounts) {
    const nameInput = el("input", { value: state.user || "", placeholder: "recorded with your corrections" });
    const dialog = modal(
      "User",
      el(
        "div",
        {},
        el("label", { class: "field" }, "Your name (recorded with every correction)", nameInput),
        el("hr", {}),
        el("p", { class: "muted small" }, "Turn on sign-in: create the administrator account. After that everyone needs an account (people can register; you approve them)."),
        el("button", { onclick: () => { dialog.close(); showLogin(); } }, "Create the administrator account…")
      ),
      [el("button", { class: "primary", onclick: () => { showUser(nameInput.value.trim()); dialog.close(); } }, "Save")]
    );
    return;
  }
  const user = auth.user;
  const dialog = modal(
    user.display || user.name,
    el(
      "div",
      { class: "col gap" },
      el("p", { class: "muted small" }, `Signed in as ${user.name} · ${user.role}`),
      el("div", { class: "row gap wrap" },
        el("button", { onclick: () => { dialog.close(); changePassword(); } }, "Change password"),
        user.role === "admin" ? el("button", { onclick: () => { dialog.close(); manageUsers(); } }, "Manage users") : null,
        el("button", { class: "danger", onclick: async () => { dialog.close(); await api("/auth/logout", { method: "POST" }); await loadAuth(); showLogin("Signed out."); } }, "Sign out")
      )
    )
  );
}

function changePassword() {
  const old = el("input", { type: "password", autocomplete: "current-password" });
  const fresh = el("input", { type: "password", autocomplete: "new-password" });
  const repeat = el("input", { type: "password", autocomplete: "new-password" });
  const dialog = modal(
    "Change password",
    el("div", {}, el("label", { class: "field" }, "Current password", old), el("label", { class: "field" }, "New password (8+ characters)", fresh), el("label", { class: "field" }, "New password again", repeat)),
    [
      el("button", { class: "ghost", onclick: () => dialog.close() }, "Cancel"),
      el("button", {
        class: "primary",
        onclick: async () => {
          if (fresh.value !== repeat.value) return toast("The two new passwords differ", "error");
          try {
            await api("/auth/password", { method: "POST", json: { old_password: old.value, new_password: fresh.value } });
            dialog.close();
            toast("Password changed; other sessions are signed out", "ok");
          } catch (error) {
            toast(error.message, "error");
          }
        },
      }, "Change"),
    ]
  );
}

async function manageUsers() {
  const body = el("div", {});
  const dialog = modal("Users", body, [], { wide: true });
  const patch = async (name, change, done) => {
    try {
      await api(`/auth/users/${encodeURIComponent(name)}`, { method: "PATCH", json: change });
      if (done) toast(done, "ok");
      render();
    } catch (error) {
      toast(error.message, "error");
    }
  };
  const render = async () => {
    let data;
    try {
      data = await api("/auth/users");
    } catch (error) {
      body.textContent = error.message;
      return;
    }
    const me = state.auth.user.name.toLowerCase();
    const reg = el("select", {}, [["approval", "Anyone can register; an admin approves"], ["open", "Anyone can register and sign in at once"], ["closed", "Closed: only admins add accounts"]].map(([v, t]) => el("option", { value: v }, t)));
    reg.value = data.registration;
    reg.addEventListener("change", async () => {
      try {
        await api("/auth/settings", { method: "PATCH", json: { registration: reg.value } });
        toast("Registration updated", "ok");
      } catch (error) {
        toast(error.message, "error");
      }
    });
    const rows = data.users.map((u) => {
      const role = el("select", { class: "small" }, el("option", { value: "reviewer" }, "reviewer"), el("option", { value: "admin" }, "admin"));
      role.value = u.role;
      role.addEventListener("change", () => patch(u.name, { role: role.value }, `${u.name} is now ${role.value}`));
      const self = u.name.toLowerCase() === me;
      return el(
        "tr",
        { class: u.active ? "" : "pending" },
        el("td", {}, el("strong", {}, u.name), u.display && u.display !== u.name ? el("div", { class: "muted small" }, u.display) : null),
        el("td", {}, role),
        el("td", {}, u.active ? "active" : el("span", { class: "chip warn" }, "waiting for approval")),
        el("td", { class: "muted small" }, u.last_login ? new Date(u.last_login * 1000).toLocaleString() : "never"),
        el(
          "td",
          {},
          el("div", { class: "row gap" },
          u.active ? (self ? null : el("button", { class: "small", onclick: () => patch(u.name, { active: false }, `${u.name} can no longer sign in`) }, "Disable")) : el("button", { class: "small primary", onclick: () => patch(u.name, { active: true }, `${u.name} approved`) }, "Approve"),
          el("button", {
            class: "small",
            onclick: () => {
              const pw = prompt(`New password for ${u.name} (8+ characters):`);
              if (pw) patch(u.name, { password: pw }, `Password reset for ${u.name}`);
            },
          }, "Reset password"),
          self ? null : el("button", {
            class: "small danger",
            onclick: async () => {
              if (!confirm(`Delete the account ${u.name}? Their corrections stay in the audit log.`)) return;
              try {
                await api(`/auth/users/${encodeURIComponent(u.name)}`, { method: "DELETE" });
                render();
              } catch (error) {
                toast(error.message, "error");
              }
            },
          }, "Delete"))
        )
      );
    });
    const newName = el("input", { placeholder: "user name" });
    const newPw = el("input", { type: "password", placeholder: "password (8+)" });
    const newRole = el("select", {}, el("option", { value: "reviewer" }, "reviewer"), el("option", { value: "admin" }, "admin"));
    const addBtn = el("button", {
      class: "primary small",
      onclick: async () => {
        try {
          await api("/auth/users", { method: "POST", json: { username: newName.value.trim(), password: newPw.value, role: newRole.value } });
          toast(`${newName.value.trim()} added`, "ok");
          render();
        } catch (error) {
          toast(error.message, "error");
        }
      },
    }, "Add");
    body.innerHTML = "";
    body.append(
      el("label", { class: "field" }, "Registration", reg),
      el("table", { class: "table auth-users" }, el("thead", {}, el("tr", {}, el("th", {}, "User"), el("th", {}, "Role"), el("th", {}, "Status"), el("th", {}, "Last sign-in"), el("th", {}, ""))), el("tbody", {}, rows)),
      el("h3", {}, "Add a user"),
      el("div", { class: "row gap wrap" }, newName, newPw, newRole, addBtn),
      el("p", { class: "muted small" }, "Admins manage users; everyone signed in can scan, review and export. Corrections are recorded under the signed-in name.")
    );
  };
  render();
  return dialog;
}
