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

function fmtSize(bytes) {
  if (!bytes) return "";
  const mb = bytes / 1_000_000;
  return mb >= 1 ? `${mb.toFixed(1)} MB` : `${(bytes / 1000).toFixed(0)} KB`;
}

function renderStlPicker(stls) {
  // Dropdown: pick a file, "Pobierz" opens it in a new tab.
  // Presupported variants are flagged with a star so the user-friendly
  // option is obvious at a glance. The folder link sits separately so
  // "give me everything" stays a one-tap action.
  const opts = stls
    .map((s, i) => {
      const star = s.presupported ? "★ " : "";
      const size = s.size ? ` — ${fmtSize(s.size)}` : "";
      return `<option value="${i}">${escapeHTML(star + s.name + size)}</option>`;
    })
    .join("");
  const word = plPlural(stls.length, "plik", "pliki", "plików");
  return `<select class="stl-select" data-role="stl-select"><option value="" disabled selected>Wybierz plik (${stls.length} ${word})…</option>${opts}</select><button type="button" class="stl-download" data-role="stl-download" disabled>Pobierz</button>`;
}

function renderCard(m) {
  const release = m.release
    ? `<span class="release-chip">${escapeHTML(m.release)}</span>`
    : "";
  const stls = m.stls || [];
  const primary = stls[0];
  const thumbInner = m.thumb
    ? `<img src="${escapeHTML(m.thumb)}" alt="${escapeHTML(m.name)}" loading="lazy">`
    : `<div class="no-thumb" aria-label="brak miniatury">${escapeHTML((m.name[0] || "?").toUpperCase())}</div>`;
  const thumbHref = primary ? primary.view_url : m.folder_url;
  const stlPicker = stls.length ? renderStlPicker(stls) : "";
  return `
    <li class="card" data-model-id="${escapeHTML(m.id)}">
      <a class="thumb-wrap" href="${escapeHTML(thumbHref)}" target="_blank" rel="noopener">
        ${thumbInner}
      </a>
      <div class="body">
        ${release}
        <h2>${escapeHTML(m.name)}</h2>
        <div class="actions">
          <div class="stl-picker">
            ${stlPicker}
          </div>
          <a class="folder-link" href="${escapeHTML(m.folder_url)}" target="_blank" rel="noopener">
            Folder na Drive ↗
          </a>
        </div>
      </div>
    </li>`;
}

function attachStlPickerHandlers() {
  // Wire each card's <select> + Pobierz button. URLs are kept off the DOM
  // by index — model lookup happens via data-model-id, so we don't need
  // to re-encode every URL into option attributes.
  $grid.querySelectorAll(".card").forEach((card) => {
    const select = card.querySelector('[data-role="stl-select"]');
    const button = card.querySelector('[data-role="stl-download"]');
    if (!select || !button) return;
    const id = card.dataset.modelId;
    const model = state.models.find((m) => m.id === id);
    if (!model) return;
    select.addEventListener("change", () => {
      button.disabled = select.value === "";
    });
    button.addEventListener("click", () => {
      const idx = parseInt(select.value, 10);
      const stl = model.stls && model.stls[idx];
      if (stl && stl.view_url) {
        window.open(stl.view_url, "_blank", "noopener");
      }
    });
  });
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
  attachStlPickerHandlers();
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
