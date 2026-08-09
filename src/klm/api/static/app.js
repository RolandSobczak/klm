// The whole client. No framework and no build step: every screen is a list or a
// form over one endpoint, and a bundler would be more machinery than the code it
// bundles. The shell holds no logic — it renders what the API says (ADR-0012).
const api = async (path, options) => {
  const response = await fetch(path, options);
  if (!response.ok) {
    const body = await response.json().catch(() => ({ detail: response.statusText }));
    throw new Error(body.detail || response.statusText);
  }
  return response.json();
};
const post = (path, body) =>
  api(path, { method: "POST", headers: { "Content-Type": "application/json" },
              body: JSON.stringify(body || {}) });

const el = (tag, attrs = {}, ...kids) => {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") node.className = v;
    else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
    else if (v !== null && v !== undefined) node.setAttribute(k, v);
  }
  for (const kid of kids.flat()) if (kid !== null && kid !== undefined)
    node.append(kid instanceof Node ? kid : document.createTextNode(String(kid)));
  return node;
};
const $ = (id) => document.getElementById(id);
const show = (id, ...nodes) => { const n = $(id); n.replaceChildren(...nodes.flat()); };
const fail = (id, error) => show(id, el("p", { class: "error" }, error.message));

// -- tabs -------------------------------------------------------------
document.querySelectorAll("#tabs button").forEach((button) => {
  button.onclick = () => {
    document.querySelectorAll("#tabs button").forEach((b) => b.classList.remove("on"));
    document.querySelectorAll(".view").forEach((v) => v.classList.remove("on"));
    button.classList.add("on");
    $(button.dataset.view).classList.add("on");
    if (button.dataset.view === "health") loadHealth();
  };
});

// -- jobs: started by a POST, watched over SSE ------------------------
function watchJob(job, label) {
  const log = el("pre");
  const card = el("div", { class: "job" }, el("strong", {}, label), log);
  $("jobs").append(card);
  const source = new EventSource(`/api/jobs/${job.id}/events`);
  source.onmessage = (event) => {
    log.textContent += JSON.parse(event.data).message + "\n";
    log.scrollTop = log.scrollHeight;
  };
  source.addEventListener("end", (event) => {
    const finished = JSON.parse(event.data);
    card.classList.add(finished.state);
    if (finished.error) log.textContent += finished.error + "\n";
    source.close();
    setTimeout(() => card.remove(), finished.state === "failed" ? 30000 : 6000);
  });
}

// -- catalog ----------------------------------------------------------
let selected = null;
async function loadParts() {
  const query = new URLSearchParams();
  if ($("search").value) query.set("q", $("search").value);
  if ($("status").value) query.set("status", $("status").value);
  try {
    const parts = await api(`/api/parts?${query}`);
    const body = $("parts").querySelector("tbody");
    body.replaceChildren(...parts.map((part) =>
      el("tr", { onclick: (e) => {
          body.querySelectorAll("tr").forEach((r) => r.classList.remove("on"));
          e.currentTarget.classList.add("on");
          loadPart(part.klm_id);
        } },
        el("td", {}, part.mpn), el("td", {}, part.manufacturer),
        el("td", {}, part.package || "—"),
        el("td", {}, el("span", { class: `tag ${part.status}` }, part.status)))));
    if (!parts.length) body.replaceChildren(el("tr", {}, el("td", { colspan: 4, class: "muted" },
      "Nothing matches. `klm import --from-kicad` brings a library in.")));
  } catch (error) { fail("detail", error); }
}

async function loadPart(id) {
  selected = id;
  try {
    const part = await api(`/api/parts/${id}`);
    const rows = [["MPN", part.mpn], ["Manufacturer", part.manufacturer],
      ["Category", part.category || "—"], ["Package", part.package || "—"],
      ["Status", part.status], ["KLM_ID", part.klm_id]];
    show("detail",
      el("h2", {}, part.mpn),
      el("p", { class: "muted" }, part.description || "No description."),
      el("dl", {}, rows.flatMap(([k, v]) => [el("dt", {}, k), el("dd", {}, v)])),
      el("div", { class: "bar" },
        el("button", { onclick: () => setStatus(id, "approved") }, "Approve"),
        el("button", { onclick: () => setStatus(id, "deprecated") }, "Deprecate")),
      el("h2", {}, "Offers"),
      part.offers.length
        ? el("table", {}, el("tbody", {}, part.offers.map((o) =>
            el("tr", {}, el("td", {}, o.supplier), el("td", {}, o.supplier_pn),
              el("td", {}, o.stock === null ? "stock unknown" : `${o.stock} in stock`)))))
        : el("p", { class: "muted" }, "No offers. `klm refresh` fetches them."),
      el("h2", {}, "Stock"),
      part.stock.length
        ? el("table", {}, el("tbody", {}, part.stock.map((s) =>
            el("tr", {}, el("td", {}, s.quantity), el("td", {}, s.location)))))
        : el("p", { class: "muted" }, "Not recorded anywhere."));
  } catch (error) { fail("detail", error); }
}

const setStatus = async (id, status) => {
  try { await post(`/api/parts/${id}/status`, { status }); await loadParts(); await loadPart(id); }
  catch (error) { fail("detail", error); }
};

$("search").oninput = loadParts;
$("status").onchange = loadParts;
$("generate").onclick = async () =>
  watchJob(await post("/api/generate"), "Rebuilding libraries");

// -- project ----------------------------------------------------------
$("project-open").onclick = async () => {
  try {
    const project = await api(`/api/projects?path=${encodeURIComponent($("project-path").value)}`);
    const nodes = [
      el("h2", {}, project.name),
      el("p", { class: "muted" },
        `${project.vendored ? "vendored" : "linked"} · ${project.schematics.length} sheet(s)` +
        (project.board ? ` · ${project.board}` : " · no board")),
      el("h2", {}, "Bill of materials"),
      el("div", { class: "wrap" }, el("table", {},
        el("thead", {}, el("tr", {}, el("th", {}, "Qty"), el("th", {}, "Value"),
          el("th", {}, "Footprint"), el("th", {}, "Designators"))),
        el("tbody", {}, project.bom.map((line) =>
          el("tr", {}, el("td", {}, line.quantity), el("td", {}, line.value),
            el("td", {}, line.footprint), el("td", {}, line.references.join(", "))))))),
    ];
    if (project.unresolved.length)
      nodes.push(el("p", { class: "warn" },
        `${project.unresolved.length} reference(s) with no catalog part: ` +
        project.unresolved.slice(0, 12).join(", ")));
    if (project.sync)
      nodes.push(el("h2", {}, "Sync"), el("table", {}, el("tbody", {},
        project.sync.map((row) => el("tr", {},
          el("td", {}, row.mpn), el("td", { class: row.state }, row.state),
          el("td", { class: "muted" }, row.detail))))));
    show("project-body", nodes);
  } catch (error) { fail("project-body", error); }
};

$("project-vendor").onclick = async () => {
  try {
    watchJob(await post("/api/projects/vendor",
      { path: $("project-path").value, allow_unresolved: true }), "Vendoring");
  } catch (error) { fail("project-body", error); }
};

$("project-verify").onclick = async () => {
  try {
    const report = await api(
      `/api/projects/verify?path=${encodeURIComponent($("project-path").value)}`);
    show("project-body",
      el("h2", {}, report.failed ? "Not self-contained" : "Verified"),
      el("table", {}, el("tbody", {}, report.checks.map((check) =>
        el("tr", {}, el("td", { class: check.status }, check.status),
          el("td", {}, check.name), el("td", { class: "muted" }, check.summary))))),
      report.findings.length
        ? el("div", {}, el("h2", {}, "Findings"), el("ul", {}, report.findings.map((f) =>
            el("li", { class: f.severity }, `${f.file || ""} ${f.message}`))))
        : null);
  } catch (error) { fail("project-body", error); }
};

// -- ordering ---------------------------------------------------------
$("plan").onclick = async () => {
  try {
    const plan = await post("/api/orders/plan",
      { build: $("build").value, projects: $("projects-root").value || "." });
    const carts = Object.entries(plan.carts).map(([name, cart]) =>
      el("div", {}, el("h2", {}, `${name} — ${cart.total.toFixed(2)} ${cart.currency} (estimate)`),
        el("ul", { class: "muted" }, cart.assumptions.map((a) => el("li", {}, a)))));
    show("orders-body",
      el("div", { class: "wrap" }, el("table", {},
        el("thead", {}, el("tr", {}, el("th", {}, "Qty"), el("th", {}, "Part"),
          el("th", {}, "Supplier"), el("th", {}, "Why"), el("th", {}, "Subtotal"))),
        el("tbody", {}, plan.lines.map((line) =>
          el("tr", { title: line.explain.join("\n") },
            el("td", {}, line.quantity), el("td", {}, line.mpn),
            el("td", {}, line.supplier), el("td", { class: "muted" }, line.reason),
            el("td", {}, line.subtotal.toFixed(2))))))),
      carts,
      plan.improvements.length
        ? el("ul", { class: "muted" }, plan.improvements.map((i) => el("li", {}, i))) : null,
      plan.unsourced.length
        ? el("p", { class: "warn" }, `No source for: ${plan.unsourced.join(", ")}`) : null,
      el("p", { class: "muted" },
        "klm does not place orders. Export the cart and submit it yourself."));
  } catch (error) { fail("orders-body", error); }
};

// -- stock ------------------------------------------------------------
$("stock-load").onclick = async () => {
  try {
    const query = $("location").value ? `?location=${encodeURIComponent($("location").value)}` : "";
    const items = await api(`/api/stock${query}`);
    show("stock-body", items.length
      ? el("table", {}, el("thead", {}, el("tr", {}, el("th", {}, "Qty"), el("th", {}, "Part"),
          el("th", {}, "Location"), el("th", {}, "Counted"))),
          el("tbody", {}, items.map((item) => el("tr", {},
            el("td", {}, item.quantity), el("td", {}, item.mpn), el("td", {}, item.location),
            el("td", { class: "muted" }, item.last_counted || "never")))))
      : el("p", { class: "muted" }, "Nothing in stock. Receiving an order is what fills this."));
  } catch (error) { fail("stock-body", error); }
};

// -- health -----------------------------------------------------------
async function loadHealth() {
  try {
    const health = await api("/api/health");
    show("health-body",
      el("dl", {}, el("dt", {}, "Catalog"), el("dd", {}, health.catalog),
        el("dt", {}, "Initialised"), el("dd", {}, String(health.initialised)),
        ...(health.parts ? Object.entries(health.parts).flatMap(([k, v]) =>
          [el("dt", {}, k), el("dd", {}, v)]) : [])),
      el("h2", {}, "External tools"),
      el("table", {}, el("tbody", {}, health.tools.map((tool) => el("tr", {},
        el("td", { class: tool.found ? "ok" : "warn" }, tool.found ? "found" : "missing"),
        el("td", {}, tool.name), el("td", {}, tool.version || ""),
        el("td", { class: "muted" }, tool.found ? "" : `${tool.consequence} — ${tool.install_hint}`)
      )))));
  } catch (error) { fail("health-body", error); }
}

loadParts();
