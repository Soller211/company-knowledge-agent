// Shared by every page: icons, API calls, sidebar and session.
const ICONS = {
  chat: '<path d="M4 5h16v11H9l-5 4z"/>',
  doc: '<path d="M6 3h8l4 4v14H6z"/><path d="M14 3v4h4"/>',
  users: '<circle cx="9" cy="8" r="3.2"/><path d="M3 20c0-3.3 2.7-5.5 6-5.5s6 2.2 6 5.5M16 5.5a3 3 0 0 1 0 5.6M18 14.8c1.8.6 3 2.4 3 5.2"/>',
  sync: '<path d="M20 12a8 8 0 0 1-14 5.3M4 12a8 8 0 0 1 14-5.3M18 3v4h-4M6 21v-4h4"/>',
  upload: '<path d="M12 16V4M7 9l5-5 5 5M4 20h16"/>',
  trash: '<path d="M4 7h16M9 7V4h6v3M6 7l1 13h10l1-13"/>',
  edit: '<path d="M4 20h4L19 9l-4-4L4 16z"/>',
  key: '<circle cx="8" cy="15" r="4"/><path d="M11 12l8-8M16 7l2 2"/>',
  logout: '<path d="M10 4H5v16h5M15 8l4 4-4 4M19 12H9"/>',
  plus: '<path d="M12 5v14M5 12h14"/>',
  menu: '<path d="M4 7h16M4 12h16M4 17h16"/>',
  send: '<path d="M4 12l16-8-6 16-2-7z"/>',
};

function icon(name) {
  const span = document.createElement("span");
  span.className = "icon";
  span.innerHTML = `<svg viewBox="0 0 24 24" aria-hidden="true" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">${ICONS[name]}</svg>`;
  return span;
}

function iconButton(name, label) {
  const button = document.createElement("button");
  button.type = "button";
  button.className = "icon-button";
  button.title = label;
  button.setAttribute("aria-label", label);
  button.append(icon(name));
  return button;
}

async function api(path, {method = "GET", json, form} = {}) {
  const options = {method, headers: {}};
  if (json !== undefined) {
    options.headers["Content-Type"] = "application/json";
    options.body = JSON.stringify(json);
  }
  if (form !== undefined) options.body = form;
  const response = await fetch(path, options);
  const data = response.status === 204 ? null : await response.json().catch(() => null);
  return {status: response.status, ok: response.ok, data};
}

function errorText(data, fallback) {
  if (data && typeof data.detail === "string") return data.detail;
  if (data && Array.isArray(data.detail) && data.detail.length) return data.detail[0].msg;
  return fallback;
}

const NAV = [
  {href: "/", label: "Chat", icon: "chat"},
  {href: "/documents", label: "Documentos", icon: "doc", admin: true},
  {href: "/users", label: "Usuarios", icon: "users", admin: true},
];

function renderSidebar(me) {
  const aside = document.getElementById("sidebar");
  aside.replaceChildren();
  document.body.classList.toggle("no-session", !me);
  if (!me) return;
  const brand = document.createElement("div");
  brand.className = "brand";
  brand.textContent = "Company Knowledge Agent";
  const nav = document.createElement("nav");
  nav.setAttribute("aria-label", "Principal");
  for (const item of NAV) {
    if (item.admin && !me.is_admin) continue;
    const link = document.createElement("a");
    link.href = item.href;
    link.append(icon(item.icon), item.label);
    if (location.pathname === item.href) link.setAttribute("aria-current", "page");
    nav.append(link);
  }
  const spacer = document.createElement("div");
  spacer.className = "spacer";
  const user = document.createElement("div");
  user.className = "user";
  const name = document.createElement("span");
  name.textContent = me.user;
  const logout = document.createElement("button");
  logout.type = "button";
  logout.className = "ghost";
  logout.append(icon("logout"), "Salir");
  logout.onclick = async () => {
    await api("/logout", {method: "POST"});
    location.href = "/";
  };
  user.append(name, logout);
  aside.append(brand, nav, spacer, user);
}

// Loads the session, draws the sidebar and returns the user (null when not logged in).
async function startPage({adminOnly = false} = {}) {
  const {status, data} = await api("/me");
  const me = status === 200 ? data : null;
  if (adminOnly && !(me && me.is_admin)) {
    location.replace("/");
    return null;
  }
  renderSidebar(me);
  return me;
}

document.addEventListener("DOMContentLoaded", () => {
  const toggle = document.getElementById("menu-toggle");
  if (!toggle) return;
  toggle.append(icon("menu"));
  toggle.onclick = () => document.body.classList.toggle("nav-open");
});
