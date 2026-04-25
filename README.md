# STL Gallery (Google Drive → GitHub Pages)

Statyczna galeria modeli 3D z Twojego Google Drive. Skaner uruchamiany przez
GitHub Actions raz dziennie schodzi po drzewie folderów (z obsługą skrótów
Drive), wybiera dla każdego modelu „pomalowane" zdjęcie (scoring kolorowości)
i generuje `manifest.json` + miniatury. Galeria hostuje się na GitHub Pages.

Pliki STL **nie są** redystrybuowane — przycisk na karcie prowadzi do
`webViewLink` w Drive z istniejącymi uprawnieniami pliku.

## Setup (jednorazowo, ~10 min)

### 1. Google Cloud — projekt + Drive API

1. Wejdź na https://console.cloud.google.com.
2. Utwórz projekt (np. `stl-gdrive-viewer`).
3. **APIs & Services → Library** → wyszukaj „Google Drive API" → **Enable**.

### 2. OAuth consent screen

1. **APIs & Services → OAuth consent screen**.
2. **External** (chyba że masz Workspace — wtedy Internal).
3. App name: `stl-gdrive-viewer`, support email = Twój.
4. Scopes: dodaj `.../auth/drive.readonly`.
5. Test users: dodaj **swój własny adres Gmail** (ten z dostępem do plików).
   Bez tego refresh token przy External wygasa po 7 dniach.
6. Save.

### 3. OAuth Desktop client

1. **APIs & Services → Credentials → + Create Credentials → OAuth client ID**.
2. Application type: **Desktop app**.
3. Po utworzeniu: **Download JSON** → zapisz jako `client_secret.json`
   w katalogu projektu (jest w `.gitignore`).

### 4. Wygeneruj refresh token lokalnie

```bash
pip install -r scanner/requirements.txt
python scanner/auth_bootstrap.py client_secret.json
```

Skrypt otworzy przeglądarkę → zaloguj się **swoim** kontem → wyraź zgodę.
Wydrukuje 3 wartości do skopiowania:

```
GOOGLE_OAUTH_CLIENT_ID=...
GOOGLE_OAUTH_CLIENT_SECRET=...
GOOGLE_OAUTH_REFRESH_TOKEN=...
```

### 5. GitHub Secrets + Variable

W repo → **Settings → Secrets and variables → Actions**:

- **Secrets** (3): `GOOGLE_OAUTH_CLIENT_ID`, `GOOGLE_OAUTH_CLIENT_SECRET`,
  `GOOGLE_OAUTH_REFRESH_TOKEN` — wartości z poprzedniego kroku.
- **Variable** (1): `GDRIVE_ROOT_FOLDER_ID` — ID głównego folderu, który
  chcesz zeskanować. Skopiuj z URL-a folderu otwartego w Drive:
  `https://drive.google.com/drive/folders/<TUTAJ>`.

### 6. Włącz GitHub Pages

**Settings → Pages → Source: GitHub Actions**.

### 7. Pierwszy run

**Actions → Refresh gallery → Run workflow**. Po sukcesie URL Pages
wyświetli się w środowisku `github-pages`.

## Lokalne uruchomienie

```bash
# wygeneruj refresh token raz (krok 4 powyżej), potem:
export GOOGLE_OAUTH_CLIENT_ID=...
export GOOGLE_OAUTH_CLIENT_SECRET=...
export GOOGLE_OAUTH_REFRESH_TOKEN=...

python -m scanner.scan \
  --root <FOLDER_ID> \
  --out site/manifest.json \
  --thumbs site/thumbs \
  --limit 5 \
  -v

python -m http.server -d site
# otwórz http://localhost:8000
```

`--limit 5` przerabia tylko pierwsze 5 modeli — wygodne do iteracji.

## Jak skaner wybiera zdjęcie i STL

- **Zdjęcie**: dla każdego kandydata liczona jest metryka
  Hasler-Süsstrunk colorfulness + średnia saturacja w HSV. Wybierane jest
  to o najwyższym wyniku (pomalowane modele wygrywają z renderami).
- **STL**: jeśli w którymś podfolderze nazwa zawiera `presupported`,
  bierzemy największy z tego zbioru. W przeciwnym razie największy w ogóle.
  W manifeście zapisujemy też `stl_count` — front pokazuje „cały folder
  na Drive" jeśli plików jest więcej.

## Struktura folderów na Drive

Skaner radzi sobie z mieszanymi strukturami — nie zakłada konkretnej
hierarchii. Walker (`scanner/walker.py`) klasyfikuje każdy folder jako:

- **model** — folder, w którego poddrzewie są STL-e i nie ma sub-modeli;
- **grupa (release)** — folder z modelami w poddrzewie, jego nazwa staje
  się etykietą `release` na kartach modeli (np. „April 2026");
- **skip** — żadnych STL-i w poddrzewie.

To znaczy że wszystkie te przypadki zadziałają:

```
April 2026/ModelA/presupported/*.stl   → ModelA, release="April 2026"
April 2026/ModelB/*.stl                → ModelB, release="April 2026"
StandaloneModel/*.stl                  → StandaloneModel, release=null
```

## Troubleshooting

- **`401` w Actions** — refresh token wygasł albo cofnięto zgodę. Powtórz
  krok 4 i zaktualizuj `GOOGLE_OAUTH_REFRESH_TOKEN`. Jeśli OAuth consent
  jest w stanie „Testing", token wygasa po 7 dniach — promote do
  **Published** żeby był długoterminowy.
- **Skaner widzi tylko skróty bez zawartości** — upewnij się, że pliki są
  shortcutami (NomNom standard) i że Drive API jest włączone w projekcie.
- **Widzowie nie mogą pobrać STL** — to zależy od uprawnień ustawionych
  przez właściciela pliku (NomNom). Galeria używa `webViewLink` —
  przekieruje do Drive z prośbą o logowanie. To zamierzone zachowanie:
  nie redystrybuujemy plików.
