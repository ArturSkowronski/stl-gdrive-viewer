const ALL = "__all__";

const state = {
  models: [],
  releases: [],
  query: "",
  release: ALL,
};

const $grid = document.getElementById("grid");
const $search = document.getElementById("search");
const $releases = document.getElementById("releases");
const $status = document.getElementById("status");
const $meta = document.getElementById("meta");

function escapeHTML(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    '"': "&quot;",
    "'": "&#39;",
  }[c]));
}

function renderChips() {
  const chips = [{ key: ALL, label: "Wszystkie" }];
  for (const r of state.releases) chips.push({ key: r, label: r });

  $releases.innerHTML = chips
    .map(
      (c) => `
        <button
          class="chip"
          data-key="${escapeHTML(c.key)}"
          aria-pressed="${state.release === c.key}">
          ${escapeHTML(c.label)}
        </button>`
    )
    .join("");

  $releases.querySelectorAll(".chip").forEach((btn) => {
    btn.addEventListener("click", () => {
      state.release = btn.dataset.key;
      renderChips();
      renderGrid();
    });
  });
}

function filtered() {
  const q = state.query.trim().toLowerCase();
  return state.models.filter((m) => {
    if (state.release !== ALL && m.release !== state.release) return false;
    if (!q) return true;
    return (
      m.name.toLowerCase().includes(q) ||
      (m.release || "").toLowerCase().includes(q)
    );
  });
}

function renderCard(m) {
  const release = m.release
    ? `<span class="release-chip">${escapeHTML(m.release)}</span>`
    : "";
  const stlExtra =
    m.stl_count > 1
      ? `<a class="folder-link" href="${escapeHTML(m.folder_url)}" target="_blank" rel="noopener">
           Cały folder na Drive (${m.stl_count} plików STL)
         </a>`
      : `<a class="folder-link" href="${escapeHTML(m.folder_url)}" target="_blank" rel="noopener">
           Cały folder na Drive
         </a>`;
  const thumbInner = m.thumb
    ? `<img src="${escapeHTML(m.thumb)}" alt="${escapeHTML(m.name)}" loading="lazy">`
    : `<div class="no-thumb" aria-label="brak miniatury">${escapeHTML((m.name[0] || "?").toUpperCase())}</div>`;
  return `
    <li class="card">
      <a class="thumb-wrap" href="${escapeHTML(m.stl.view_url)}" target="_blank" rel="noopener">
        ${thumbInner}
      </a>
      <div class="body">
        ${release}
        <h2>${escapeHTML(m.name)}</h2>
        <div class="actions">
          <a class="button" href="${escapeHTML(m.stl.view_url)}" target="_blank" rel="noopener">
            Otwórz STL na Drive
          </a>
          ${stlExtra}
        </div>
      </div>
    </li>`;
}

function renderGrid() {
  const items = filtered();
  $grid.removeAttribute("aria-busy");
  if (!items.length) {
    $grid.innerHTML = "";
    $status.hidden = false;
    $status.textContent = state.models.length
      ? "Nic nie pasuje do filtrów."
      : "Galeria jest pusta.";
    return;
  }
  $status.hidden = true;
  $grid.innerHTML = items.map(renderCard).join("");
}

function plPlural(n, one, few, many) {
  if (n === 1) return one;
  const mod10 = n % 10;
  const mod100 = n % 100;
  if (mod10 >= 2 && mod10 <= 4 && (mod100 < 12 || mod100 > 14)) return few;
  return many;
}

async function load() {
  try {
    const resp = await fetch("manifest.json", { cache: "no-cache" });
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    const data = await resp.json();
    state.models = data.models || [];
    state.releases = data.releases || [];
    if (data.generated_at) {
      const d = new Date(data.generated_at);
      const n = state.models.length;
      const word = plPlural(n, "model", "modele", "modeli");
      $meta.textContent = `Aktualizacja: ${d.toLocaleString()} · ${n} ${word}`;
    }
  } catch (err) {
    $grid.innerHTML = "";
    $status.hidden = false;
    $status.textContent = `Nie udało się załadować galerii: ${err.message}`;
    return;
  }
  renderChips();
  renderGrid();
}

$search.addEventListener("input", () => {
  state.query = $search.value;
  renderGrid();
});

load();
