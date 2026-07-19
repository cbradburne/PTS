/* PTS manual — shared sidebar + prev/next, generated from one page list so
   nav lives in exactly one place.  Add a page here and it appears everywhere. */
const PAGES = [
  { group: "Start here" },
  { href: "index.html",         title: "Manual home" },
  { href: "getting-started.html", title: "Getting started" },
  { href: "concepts.html",      title: "How it fits together" },

  { group: "Using the rig" },
  { href: "mounts.html",        title: "The mounts" },
  { href: "positions.html",     title: "Storing & recalling shots" },
  { href: "lookat.html",        title: "Look-at tracking" },
  { href: "cv.html",            title: "CV tracking" },
  { href: "names.html",         title: "Camera & position names" },

  { group: "Control surfaces" },
  { href: "pc-app.html",        title: "PC app" },
  { href: "web-app.html",       title: "Web app" },
  { href: "hub-display.html",   title: "Hub touchscreen" },
  { href: "companion.html",     title: "Companion / QLab (OSC)" },

  { group: "Setup & reference" },
  { href: "pairing.html",       title: "Pairing & identity" },
  { href: "config.html",        title: "Configuration reference" },
  { href: "building.html",      title: "Building the firmware" },
  { href: "troubleshooting.html", title: "Troubleshooting" },
];

function currentFile() {
  const p = location.pathname.split("/").pop();
  return p === "" ? "index.html" : p;
}

function buildSidebar() {
  const cur = currentFile();
  const side = document.getElementById("sidebar");
  const nav = document.createElement("nav");
  for (const item of PAGES) {
    if (item.group) {
      const g = document.createElement("div");
      g.className = "grouplabel"; g.textContent = item.group;
      nav.appendChild(g);
      continue;
    }
    const a = document.createElement("a");
    a.href = item.href; a.textContent = item.title;
    if (item.href === cur) a.className = "current";
    nav.appendChild(a);
  }
  side.appendChild(nav);
}

function buildPager() {
  const links = PAGES.filter(p => p.href);
  const cur = currentFile();
  const i = links.findIndex(p => p.href === cur);
  if (i < 0) return;
  const main = document.querySelector("main.doc");
  if (!main) return;
  const pager = document.createElement("div");
  pager.className = "pager";
  const prev = links[i - 1], next = links[i + 1];
  pager.innerHTML =
    (prev ? `<a href="${prev.href}"><small>← Previous</small>${prev.title}</a>` : `<span></span>`) +
    (next ? `<a class="nxt" href="${next.href}"><small>Next →</small>${next.title}</a>` : `<span></span>`);
  main.appendChild(pager);
}

document.addEventListener("DOMContentLoaded", () => {
  buildSidebar();
  buildPager();
  const btn = document.getElementById("menu-toggle");
  if (btn) btn.addEventListener("click", () =>
    document.getElementById("sidebar").classList.toggle("open"));
});
