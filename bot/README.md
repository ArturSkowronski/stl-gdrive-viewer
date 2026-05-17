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

## Raspberry Pi — instalacja krok po kroku

Wszystko poniżej dla **Raspberry Pi 4 / 5 z 64-bit Raspberry Pi OS**
(ARM64). Pi 3B+ działa wolniej ale też się skompiluje. Pi Zero (ARMv6)
nie poleca — TBA server tej architektury nie obsługuje.

### 0. Hardware checklist

| Zasób | Minimum | Zalecane |
|---|---|---|
| RAM | 2 GB | 4 GB+ (Pi 4/5) |
| Storage | 32 GB SD | 64 GB SD + zewnętrzny SSD na USB do downloads |
| Sieć | dowolna | przewodowa eth dla stabilności uploadów |
| OS | RPi OS 64-bit Bookworm | jak Minimum |

Bot pobiera multi-GB rary do volume'u `tba-data`. Jeśli zostawisz to
na karcie SD, w ciągu kilku tygodni może się zapełnić — łatwiej
zamontować SSD przez USB i przekierować volume tam.

### 1. Świeży system + Docker

```bash
sudo apt update && sudo apt upgrade -y
sudo apt install -y git curl ca-certificates

# Oficjalny installer Dockera dla ARM (działa na RPi OS / Ubuntu)
curl -fsSL https://get.docker.com | sudo sh

# Pozwól swojemu userowi używać dockera bez sudo
sudo usermod -aG docker $USER
newgrp docker  # reload grup w bieżącej sesji

# Sanity check
docker run --rm hello-world
```

### 2. (Opcjonalnie) SSD na downloads

Jeśli masz zewnętrzny SSD wpięty w USB 3.0:

```bash
# Zobacz urządzenie (zwykle /dev/sda1)
lsblk

# Sformatuj na ext4 (UWAGA: kasuje dane!)
sudo mkfs.ext4 /dev/sda1

# Zamontuj na stałe
sudo mkdir -p /mnt/stl-data
sudo blkid /dev/sda1  # skopiuj UUID
echo "UUID=<TUTAJ> /mnt/stl-data ext4 defaults,noatime 0 2" | sudo tee -a /etc/fstab
sudo mount -a
sudo chown -R $USER:$USER /mnt/stl-data
```

W `docker-compose.yml` zmień volume mapping:

```yaml
volumes:
  - /mnt/stl-data/tba:/var/lib/telegram-bot-api
```

Zamiast nazwanego `tba-data` — Docker będzie pisać prosto na SSD.

### 3. Pobierz repo i bot

```bash
cd ~
git clone https://github.com/ArturSkowronski/stl-gdrive-viewer.git
cd stl-gdrive-viewer/bot
```

### 4. Wygeneruj credentials (na laptopie, nie na Pi)

Pi nie ma przeglądarki — bootstrap odpalany na komputerze z GUI, potem
przepisz wartości do `.env` na Pi.

Na **laptopie**:

```bash
# Klon repo, środowisko Pythona
pip install -r scanner/requirements.txt

# Drive write token (osobny od scanner readonly!)
python scanner/auth_bootstrap.py path/to/client_secret.json --write
# Skopiuj GOOGLE_OAUTH_CLIENT_ID / _SECRET / _REFRESH_TOKEN
```

`@BotFather` na Telegramie:

```
/newbot   → wybierz nazwę i username, skopiuj HTTP API token
/setprivacy → twojego bota → Disable
```

`https://my.telegram.org` → API development tools → Create new app
→ skopiuj API_ID i API_HASH.

Forward dowolnej wiadomości do `@userinfobot` → twój numeric user ID.

### 5. Skonfiguruj `.env` na Pi

```bash
cp .env.example .env
nano .env
```

Wklej wszystkie wartości z kroku 4. **`ALLOWED_USER_IDS` to twój numeric
ID** — bez tego każdy kto trafi na bota mógłby uploadować w twój Drive.

### 6. Pierwsze uruchomienie

```bash
docker compose up -d --build
docker compose logs -f bot
```

Pierwszy build na Pi 4 trwa ~5 minut (instaluje pip deps + bs4 +
google-api-python-client kompiluje się natywnie). Kolejne restarty
wstają w ~10 sekund.

Powinno wyświetlić `bot starting — work dir=...` i potem ciszę. Forwarduj
dowolnego rara do bota — powinieneś dostać `📥 → ☁️ → ✅ {url}` w czacie.

### 7. Auto-start przy boocie (systemd)

Docker compose sam się NIE wstaje po reboot bez systemd unitu. Stwórz:

```bash
sudo tee /etc/systemd/system/stl-bot.service > /dev/null <<'EOF'
[Unit]
Description=STL Telegram-to-Drive bot
Requires=docker.service
After=docker.service network-online.target

[Service]
Type=oneshot
RemainAfterExit=true
WorkingDirectory=/home/pi/stl-gdrive-viewer/bot
ExecStart=/usr/bin/docker compose up -d
ExecStop=/usr/bin/docker compose down
User=pi

[Install]
WantedBy=multi-user.target
EOF

# Dopasuj WorkingDirectory + User=pi do swojej nazwy użytkownika
sudo systemctl daemon-reload
sudo systemctl enable --now stl-bot.service
sudo systemctl status stl-bot.service
```

Po reboot Pi (`sudo reboot`) bot wstanie automatycznie. Sprawdź
`systemctl status stl-bot` i `docker compose logs -f bot` — jeśli widać
"bot starting", wszystko OK.

### 8. Aktualizacje

```bash
cd ~/stl-gdrive-viewer
git pull
cd bot
docker compose up -d --build
```

Push do `main` na GitHubie nie wpływa na Pi automatycznie — musisz
ręcznie `git pull` + rebuild. Jeśli chcesz auto-update, dodaj cronjob:

```bash
crontab -e
# co niedzielę o 03:00:
0 3 * * 0 cd /home/pi/stl-gdrive-viewer && git pull && cd bot && docker compose up -d --build >/dev/null 2>&1
```

### Troubleshooting na Pi

**`bot exits immediately`** — sprawdź `docker compose logs bot`. Najczęściej
brakuje któregoś sekretu w `.env` (`KeyError`).

**`tba is unhealthy`** — TBA server potrzebuje 30-60s na pierwszy start
(generuje sesję, ściąga DC info). Healthcheck w compose ma 30s grace.
Jeśli dalej fail, sprawdź `docker compose logs tba` — najczęściej zły
`TELEGRAM_API_ID/HASH`.

**Upload się wiesza w połowie** — Drive resumable upload retry-uje
automatycznie, ale wolne łącze + 1.5 GB rar = 20+ minut. Cierpliwość.
Jeśli zawisa na stałe, restart compose i forward ponownie (bot jest
idempotentny — wyłapie że plik już jest w połowie i… nie, nieprawda,
zacznie od nowa — Drive resumable session żyje 7 dni ale bot trzyma
session token w pamięci, restart = od zera).

**RAM 2 GB i OOM** — Docker compose limit dla bota: dodaj do
`docker-compose.yml` pod `bot:`:

```yaml
deploy:
  resources:
    limits:
      memory: 512M
```

Większość uploadu chodzi przez resumable chunks po 16 MB, więc 512 MB
spokojnie wystarczy.

**SD card się zapełnia** — `docker system prune -a` od czasu do czasu
+ rozważ SSD (krok 2).


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
