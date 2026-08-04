/* AI4IA site — shared rendering logic. Loaded as an EXTERNAL script (no inline JS) so
   it also works under the app's strict nonce/strict-dynamic CSP if the site is served
   from the web container. Reads the window.AI4IA_* data globals and renders per page
   by detecting which container elements exist. */
(function () {
  "use strict";

  // ---------- helpers ----------
  function esc(s) {
    return String(s == null ? "" : s)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
  }
  function el(id) { return document.getElementById(id); }
  function h(html) { const t = document.createElement("template"); t.innerHTML = html.trim(); return t.content; }

  function stateBadge(state, label) {
    var map = {
      healthy: "ok", up: "ok", ok: "ok",
      // "provisioned" means the resource exists and is not failed, but Resource
      // Health published no availability state for it. That is NOT a positive
      // health signal, so it must not render as a green "ok" badge.
      provisioned: "unknown",
      degraded: "warn", unknown: "unknown", unavailable: "bad", down: "bad"
    };
    var cls = map[state] || "unknown";
    var text = label || state;
    return '<span class="badge ' + cls + '">' + esc(text) + "</span>";
  }

  function fmtDate(iso) {
    if (!iso) return "unknown";
    var d = new Date(iso);
    if (isNaN(d)) return esc(iso);
    var diff = Date.now() - d.getTime();
    var mins = Math.round(diff / 60000);
    var rel;
    if (mins < 1) rel = "just now";
    else if (mins < 60) rel = mins + " min ago";
    else if (mins < 1440) rel = Math.round(mins / 60) + " h ago";
    else rel = Math.round(mins / 1440) + " d ago";
    return d.toUTCString().replace("GMT", "UTC") + " (" + rel + ")";
  }

  function groupBy(arr, key) {
    return arr.reduce(function (acc, x) { (acc[x[key]] = acc[x[key]] || []).push(x); return acc; }, {});
  }

  // ---------- index.html ----------
  function renderMeta() {
    var m = window.AI4IA_META;
    if (!m) return;
    var feat = el("features");
    if (feat) {
      feat.innerHTML = m.features.map(function (f) {
        var badge = f.on ? stateBadge("healthy", "on") : stateBadge("unknown", "off");
        var core = f.core ? ' <span class="tag">core</span>' : "";
        return '<div class="feat"><div class="state">' + badge + '</div><div class="body"><strong>' +
          esc(f.name) + core + "</strong>" + (f.note ? "<span>" + esc(f.note) + "</span>" : "") + "</div></div>";
      }).join("");
    }
    var stack = el("stack");
    if (stack) {
      stack.innerHTML = m.stack.map(function (s) {
        return "<tr><td><strong>" + esc(s.layer) + "</strong></td><td>" + esc(s.tech) +
          '</td><td class="mono">' + esc(s.host) + "</td></tr>";
      }).join("");
    }
    var regions = el("regions");
    if (regions) {
      regions.innerHTML = m.environment.regions.map(function (r) {
        return '<div class="card"><h3>📍 ' + esc(r.region) + "</h3><p>" + esc(r.role) + "</p></div>";
      }).join("");
    }
    var envfacts = el("envfacts");
    if (envfacts) {
      var e = m.environment;
      var rows = [
        ["Environment", e.envName + " (" + e.appEnvironment + ")"],
        ["Authentication", e.authProvider],
        ["Primary region", e.primaryRegion],
        ["Resource group", e.resourceGroup],
        ["Subscription", e.subscription],
      ];
      envfacts.innerHTML = rows.map(function (r) {
        return "<tr><td>" + esc(r[0]) + '</td><td class="mono">' + esc(r[1]) + "</td></tr>";
      }).join("");
    }
  }

  // ---------- status.html ----------
  function renderStatus() {
    var s = window.AI4IA_STATUS;
    var host = el("status-stats");
    if (!s || !host) return;
    var up = el("updated");
    if (up) up.textContent = "Snapshot generated " + fmtDate(s.generatedAt);

    var sum = s.summary;
    var resources = s.resources || [];

    // Derive the displayed state from the availability signal rather than
    // trusting the stored `state`. Two reasons:
    //  1. snapshots generated before the healthy/provisioned split recorded
    //     "healthy" for every resource that merely existed, which is how the
    //     page came to claim "33 Healthy" while all 33 rows read
    //     "Availability: Unknown"; and
    //  2. it keeps the rendering honest even if the generator regresses.
    // Failure states still win -- those are real signals.
    function effectiveState(r) {
      if (r.state === "unavailable" || r.state === "degraded") return r.state;
      return String(r.availability || "").toLowerCase() === "available"
        ? "healthy"
        : "provisioned";
    }

    var counts = { healthy: 0, provisioned: 0, degraded: 0, unavailable: 0 };
    resources.forEach(function (r) { counts[effectiveState(r)]++; });

    var stats = el("status-stats");
    if (stats) {
      // "Health reported" is deliberately separate from "Provisioned": most Azure
      // resource types publish no Resource Health availability state at all, and
      // counting those as healthy turned an absent signal into a green number.
      stats.innerHTML =
        stat(resources.length || sum.total, "Azure resources") +
        stat(counts.healthy, "Health reported available") +
        stat(counts.provisioned, "Provisioned, no health signal") +
        stat(sum.endpointsUp + "/" + sum.endpointsTot, "Public endpoints up") +
        stat(counts.degraded + counts.unavailable, "Degraded / unavailable");
    }

    var eps = el("endpoints");
    if (eps) {
      eps.innerHTML = s.endpoints.map(function (e) {
        var b = stateBadge(e.state, e.state === "up" ? "reachable" : (e.state === "unknown" ? "no response" : "down"));
        return '<div class="card"><h3>' + b + " " + esc(e.name) + '</h3><p class="mono">' + esc(e.url) +
          "</p><p>HTTP " + esc(e.httpStatus || "—") + (e.note ? " · " + esc(e.note) : "") + "</p></div>";
      }).join("");
    }

    var body = el("resources-body");
    if (body) {
      var groups = groupBy(resources, "group");
      var html = "";
      Object.keys(groups).sort().forEach(function (g) {
        html += '<tr class="grouprow"><td colspan="5"><strong>' + esc(g) + "</strong></td></tr>";
        groups[g].forEach(function (r) {
          html += "<tr><td>" + stateBadge(effectiveState(r)) + "</td><td><strong>" + esc(r.label) +
            '</strong><br><span class="mono">' + esc(r.name) + "</span></td><td>" + esc(r.location) +
            '</td><td class="mono">' + esc(r.provisioningState) + "</td><td>" + esc(r.availability) + "</td></tr>";
        });
      });
      body.innerHTML = html;
    }
  }
  function stat(n, l) { return '<div class="stat"><div class="n">' + esc(n) + '</div><div class="l">' + esc(l) + "</div></div>"; }

  // ---------- services.html ----------
  function renderServices() {
    var svc = window.AI4IA_SERVICES;
    var host = el("services-root");
    if (!svc || !host) return;
    // Count live instances per service using the generated inventory (best effort).
    var inv = (window.AI4IA_INVENTORY && window.AI4IA_INVENTORY.resources) || [];
    // Match on BOTH the Azure type and the service's own name pattern. Matching
    // type alone made every service sharing a type report the same number, so the
    // web app, API and model proxy each claimed "3 live" when exactly one
    // container app matches each of ca-web-*, ca-api-* and ca-proxy-*.
    // `resourcePattern` uses shell-style `*` plus `{a,b}`/`{region}` placeholders,
    // both of which become "any run of characters" here -- deliberately loose,
    // because this is a display hint, not an authorization decision.
    function patternToRegExp(pattern) {
      var escaped = String(pattern).replace(/[.+^$()|[\]\\]/g, "\\$&");
      var body = escaped.replace(/\{[^}]*\}/g, "*").replace(/\*/g, ".*");
      return new RegExp("^" + body + "$", "i");
    }
    function liveCount(service) {
      var t = String(service.azureType).toLowerCase();
      var rx = patternToRegExp(service.resourcePattern);
      return inv.filter(function (r) {
        return String(r.type).toLowerCase() === t && rx.test(String(r.name));
      }).length;
    }
    var groups = groupBy(svc, "group");
    var html = "";
    Object.keys(groups).sort().forEach(function (g) {
      html += '<h2 class="group-title">' + esc(g) + "</h2><div class='grid cols-2'>";
      groups[g].forEach(function (s) {
        var n = liveCount(s);
        var docs = (s.docs || []).map(function (d) { return '<a href="' + esc(d[1]) + '" target="_blank" rel="noopener">' + esc(d[0]) + "</a>"; }).join(" · ");
        html += '<div class="card"><h3><span class="icon">' + esc(s.icon) + "</span> " + esc(s.name) +
          (n ? ' <span class="tag">' + n + " live</span>" : "") + "</h3>" +
          "<p>" + esc(s.summary) + "</p>" +
          '<p class="meta"><strong>Azure type:</strong> <span class="mono">' + esc(s.azureType) + "</span></p>" +
          '<p class="meta"><strong>IaC:</strong> <span class="mono">' + esc(s.module) + '</span> · <strong>Names:</strong> <span class="mono">' + esc(s.resourcePattern) + "</span></p>" +
          '<p class="meta"><strong>Identity/RBAC:</strong> ' + esc(s.identity) + "</p>" +
          (docs ? '<p class="meta">📚 ' + docs + "</p>" : "") + "</div>";
      });
      html += "</div>";
    });
    host.innerHTML = html;
  }

  // ---------- requirements.html ----------
  function renderRequirements() {
    var r = window.AI4IA_REQUIREMENTS;
    if (!r) return;
    var mods = el("modules");
    if (mods) {
      mods.innerHTML = r.iac.modules.map(function (m) {
        return '<tr><td class="mono">' + esc(m.name) + "</td><td>" + esc(m.purpose) + "</td></tr>";
      }).join("");
    }
    var iacmeta = el("iac-meta");
    if (iacmeta) {
      iacmeta.innerHTML = "<p><strong>Provider:</strong> " + esc(r.iac.provider) + "</p><p><strong>Entry point:</strong> " +
        esc(r.iac.entry) + "</p><p><strong>Catalog:</strong> " + esc(r.iac.catalog) + "</p>";
    }
    var rbac = el("rbac");
    if (rbac) {
      rbac.innerHTML = r.rbac.map(function (x) {
        return '<tr><td class="mono">' + esc(x.assignee) + "</td><td><strong>" + esc(x.role) + "</strong></td><td>" +
          esc(x.scope) + "</td><td>" + esc(x.why) + '</td><td class="mono">' + esc(x.module) + "</td></tr>";
      }).join("");
    }
    var pk = el("packages");
    if (pk) {
      pk.innerHTML = ["api", "web", "proxy"].map(function (k) {
        var p = r.packages[k];
        if (!p) return "";
        var items = (p.items || []).map(function (i) {
          return "<tr><td class='mono'>" + esc(i[0]) + "</td><td>" + esc(i[1]) + "</td></tr>";
        }).join("");
        var dev = (p.dev && p.dev.length) ? '<p class="meta"><strong>Dev/tooling:</strong> ' + p.dev.map(esc).join(" · ") + "</p>" : "";
        var extra = p.extra ? '<p class="meta"><strong>Extra:</strong> ' + esc(p.extra) + "</p>" : "";
        return '<div class="card"><h3>' + esc(k.toUpperCase()) + '</h3><p class="meta"><strong>Runtime:</strong> ' + esc(p.runtime) +
          " · <strong>Manager:</strong> " + esc(p.manager) + "</p>" +
          (items ? '<div class="table-wrap"><table><tbody>' + items + "</tbody></table></div>" : "") + dev + extra + "</div>";
      }).join("");
    }
    var pre = el("prereqs");
    if (pre) {
      pre.innerHTML = r.prerequisites.map(function (p) { return "<li>" + esc(p) + "</li>"; }).join("");
    }
  }

  // ---------- docs.html ----------
  function renderDocs() {
    var d = window.AI4IA_DOCS;
    var host = el("docs-root");
    if (!d || !host) return;
    host.innerHTML = d.sections.map(function (sec) {
      var cards = sec.docs.map(function (doc) {
        var url = d.repoBase + doc.path;
        return '<a class="card" href="' + esc(url) + '" target="_blank" rel="noopener" style="display:block"><h3>📄 ' +
          esc(doc.title) + "</h3><p>" + esc(doc.desc) + '</p><p class="meta mono">' + esc(doc.path) + "</p></a>";
      }).join("");
      return '<h2 class="group-title">' + esc(sec.group) + "</h2><div class='grid cols-3'>" + cards + "</div>";
    }).join("");
  }

  // ---------- diagrams ----------
  function initMermaid() {
    if (!window.mermaid || !document.querySelector(".mermaid")) return;
    var dark = window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches;
    try {
      window.mermaid.initialize({ startOnLoad: false, theme: dark ? "dark" : "default", securityLevel: "strict", fontFamily: "Segoe UI, system-ui, sans-serif" });
      window.mermaid.run();
    } catch (e) { /* diagrams degrade to their source text */ }
  }

  document.addEventListener("DOMContentLoaded", function () {
    renderMeta();
    renderStatus();
    renderServices();
    renderRequirements();
    renderDocs();
    initMermaid();
    var y = el("year"); if (y) y.textContent = String(new Date().getFullYear());
  });
})();
