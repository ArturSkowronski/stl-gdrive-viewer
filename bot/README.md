# STL Gallery — Telegram → Drive bot

Forwarduj wiadomość z modelem (image + .rar/.zip/.stl) na czat z botem,
bot wrzuca pliki na twój Drive, następny daily refresh galerii podłapuje
nowy model jak każdy inny folder. Brak gałęzi w UI — to ten sam pipeline
co istniejący Drive flow, tylko skrótem przez Telegram.

## Wymagania

- Docker + Docker Compose (lokalnie / homeserver / Fly.io / Railway)
- Bot Telegram (`@BotFather` → `/newbot`)
- API credentials z `https://my.telegram.org` (potrzebne dla self-hosted
  Telegram Bot API server, który omija limit 20 MB na `getFile`)
- Konto Google z dostępem do Drive root foldera + OAuth refresh token
  z **write** scope (osobny od scanner-owego readonly)

## Setup krok po kroku

### 1. Stwórz bota i ustaw privacy

```
/newbot           # przy @BotFather
# wpisz nazwę i username, skopiuj HTTP API token
/setprivacy       # → twojego bota → Disable
                  # (żeby bot widział forwardy bez @mention)
```

Skopiuj token → `TELEGRAM_BOT_TOKEN` w `.env`.

### 2. Wygeneruj API_ID / API_HASH

1. Wejdź na <https://my.telegram.org> (logowanie kodem SMS na twój
   numer Telegrama)
2. **API development tools** → **Create new application**
3. App title: `stl-gallery-bot`, platform: `Other`
4. Skopiuj `App api_id` i `App api_hash` → odpowiednio
   `TELEGRAM_API_ID` i `TELEGRAM_API_HASH` w `.env`

### 3. Wygeneruj Drive write token

Scanner używa readonly tokena. Bot potrzebuje write — generujesz osobny
przez ten sam `auth_bootstrap.py`, dodając `--write`:

```bash
pip install -r scanner/requirements.txt
python scanner/auth_bootstrap.py path/to/client_secret.json --write
```

Skrypt otwiera przeglądarkę → wyrażasz zgodę na pełen Drive scope →
wypisuje 3 wartości. Wklej do `.env` jako:

```
GOOGLE_OAUTH_CLIENT_ID=...
GOOGLE_OAUTH_CLIENT_SECRET=...
GOOGLE_OAUTH_REFRESH_TOKEN=...
```

Twój istniejący readonly token (używany przez galerię w GitHub Actions)
zostaje niezmieniony. To dwa osobne setupy.

### 4. Ustaw ALLOWED_USER_IDS

Forwarduj dowolną wiadomość do `@userinfobot` → dostaniesz swoje
numeric user ID. Wpisz do `.env`:

```
ALLOWED_USER_IDS=123456789
```

Bot będzie ignorować forwardy od kogokolwiek innego — bardzo zalecane,
bo każdy kto trafi na URL twojego bota mógłby śmiecić w twoim Drive.

### 5. DRIVE_ROOT_FOLDER_ID

Ta sama wartość co `vars.GDRIVE_ROOT_FOLDER_ID` w GitHub repo (folder
który już skanuje daily refresh). Bot tworzy nowe pod-foldery TAM —
walker indeksuje je automatycznie.

### 6. Uruchom

```bash
cd bot/
cp .env.example .env       # wypełnij wartości
docker compose up -d --build
docker compose logs -f bot
```

Powinno wyświetlić `bot starting — work dir=...` po kilku sekundach
i potem ciszę, aż coś zforwardujesz.

## Użycie

1. W Telegramie znajdź swojego bota (`@TwojUsername_bot` z BotFathera)
2. **Forwarduj** wiadomość z kanału, która ma image + plik archiwum
3. Bot odpowiada:
   - `📥 Mithril Helmet ... pobieram…`
   - `☁️ Mithril Helmet ... wrzucam na Drive…`
   - `✅ Mithril Helmet — https://drive.google.com/drive/folders/...`
4. Następny daily refresh (Mon–Sat 02:00 UTC) podłapie nowy folder

Re-forwardowanie tego samego pliku → `ℹ️ X już jest na Drive`, bez
ponownego uploadu. Folder na Drive nazwany czystym display_name
(stripped extension, stripped author handle, glue underscores).

Forwardowanie media-group (album: kilka zdjęć + plik) → bot buforuje
przez 2s, łączy w jeden upload (pierwsze zdjęcie jako cover, plik
jako model). Pojedyncze wiadomości z plikiem też działają — wtedy
cover może być w samej wiadomości z plikiem albo brak.

## Hosting w cloudzie (Fly.io)

Compose-up działa lokalnie / na homeserverze. Dla Fly.io:

```bash
fly launch --no-deploy
# edit fly.toml: secrets-only env, processes = [tba, bot], volume mount
fly volume create tba_data --size 50
fly secrets set TELEGRAM_BOT_TOKEN=... TELEGRAM_API_ID=... # itd.
fly deploy
```

Detale są poza zakresem tego README — generic Fly compose docs się
stosują. Pamiętaj: TBA potrzebuje persistent volume (cached sessions),
bot potrzebuje read access do tego samego volumu (downloaded files).

## Limity i caveats

- **Telegram premium nie wymagane** — self-hosted TBA server omija
  20 MB limit Bot API. Plik 1.4 GB poszedł u mnie w testach bez
  problemów.
- **Drive 15 GB free quota** — multi-GB rary szybko zjedzą bezpłatny
  limit. Jeśli skończy się miejsce, upload się wywali z `quotaExceeded`
  i bot odpowie błędem w czacie.
- **Idempotency** — bot sprawdza `name == filename AND parent ==
  cleaned_folder` przed uploadem. Forward tego samego pliku → skip.
  Forward TEGO SAMEGO MODELU z innym plikiem (np. presupported + raw)
  → dwa pliki w jednym folderze (zgodnie z konwencją scannera).
- **Brak retry przy crashach** — jeśli bot pada w trakcie uploadu, plik
  pozostaje w trakcie. Restart i ponowny forward.
- **Bot widzi tylko forwardy do PRIVATE chat** — domyślnie. Żeby
  działał w grupie, musiałbyś go dodać + dać uprawnienia + zmienić
  filtry w `worker.py`.
