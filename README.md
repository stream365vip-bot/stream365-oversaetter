# stream365-oversaetter

Et lille program, der sørger for, at film og serier uden danske undertekster får en dansk oversættelse af høj kvalitet.

## Hvad gør programmet?

Hvert kvarter gør programmet dette:

1. Spørger Bazarr, hvilke film og afsnit der mangler danske undertekster.
2. Vælger kun:
   - film og serier, som nogen har ønsket via **Seerr** – når filen har ligget i mindst 1 time (så Bazarr selv har haft tid til at finde danske undertekster), og
   - afsnit af serier, der har mærket **oversaet** i Sonarr – når afsnittet har ligget i mindst 48 timer.
3. Springer over, hvis der allerede findes danske undertekster (som fil eller inde i videoen).
4. Finder de engelske undertekster og beder Bazarr oversætte dem til dansk (Bazarr bruger Lingarr og Claude Haiku).
5. Når den danske fil er klar, bliver den tjekket:
   - **Gratis kontrol** af hver replik (engelske rester, norske/svenske ord, for lange linjer, manglende tal osv.).
   - **Korrektur med Claude Sonnet**, som kun retter rigtige fejl.
   - Bazarrs linje "Subtitles translated with ..." erstattes med **"Oversat af Claude for ≋"**.
6. Husker i en lille database, hvad der er lavet, så intet bliver oversat to gange.

Programmet ændrer ingen indstillinger i Bazarr, Lingarr, Radarr, Sonarr eller Seerr, sletter aldrig filer og rører kun den nye danske undertekstfil.

Udgifterne til Sonnet har et loft (standard 5 dollars om måneden). Når loftet er nået, bliver Haikus oversættelse gemt uden korrektur.

## Installation

Alle kommandoer køres på serveren.

```bash
# 1. Hent programmet
sudo git clone https://github.com/stream365vip-bot/stream365-oversaetter.git /opt/oversaetter
cd /opt/oversaetter

# 2. Lav mappen til databasen
sudo mkdir -p /opt/oversaetter/data
sudo chown -R 1000:1000 /opt/oversaetter/data

# 3. Lav din egen indstillingsfil
sudo cp .env.example .env
sudo nano .env
```

## Sådan udfylder du .env

Åbn `.env` og udfyld disse felter (resten kan blive stående):

| Felt | Hvor finder jeg det? |
|---|---|
| `BAZARR_API_KEY` | Bazarr → Settings → General → Security → API Key |
| `RADARR_API_KEY` | Radarr → Settings → General → API Key |
| `SONARR_API_KEY` | Sonarr → Settings → General → API Key |
| `SEERR_URL` | Adressen på Seerr på Docker-netværket, fx `http://seerr:5055` |
| `SEERR_API_KEY` | Seerr → Settings → General → API Key |

Anthropic-nøglen skal **ikke** skrives i `.env`. Programmet læser den fra Lingarrs fil `/opt/lingarr/secrets/anthropic_api_key.txt`.

Gem filen i nano med `Ctrl+O`, `Enter`, og luk med `Ctrl+X`.

## Start og stop

```bash
cd /opt/oversaetter

# Start (første gang og efter opdatering)
sudo docker compose up -d --build

# Stop
sudo docker compose down

# Genstart (fx efter ændring i .env)
sudo docker compose up -d
```

## Se loggen

```bash
sudo docker logs -f oversaetter
```

Tryk `Ctrl+C` for at stoppe visningen (programmet kører videre). Hver linje er én hændelse, fx:

```
2026-10-16 20:15:00 INFO Runde: 4 mangler dansk, 1 kandidater, 2 venter på forsinkelse, 1 ikke valgt, ...
2026-10-16 20:15:00 INFO Beder Bazarr oversætte Filmnavn (Seerr, forsøg 1) fra ekstern engelsk: Filmnavn.en.srt
2026-10-16 20:31:12 INFO Færdig: Filmnavn - flag før: 23, flag efter: 2, rettelser: 41, pris: 0.3100 USD, ...
```

## Prøvetilstand (DRY_RUN)

Programmet starter i prøvetilstand (`DRY_RUN=true`). Så skriver det kun i loggen, hvad det **ville** gøre – det beder ikke Bazarr oversætte og skriver ingen filer.

Når loggen ser rigtig ud, slår du prøvetilstanden fra:

1. Åbn `.env`: `sudo nano /opt/oversaetter/.env`
2. Ret linjen til `DRY_RUN=false`
3. Gem og genstart: `sudo docker compose up -d`

## Testtilstand: prøv efterbehandlingen på to filer

Du kan prøve kontrollen, korrekturen og kreditlinjen på en engelsk og en dansk undertekstfil – uden at noget andet bliver rørt. Resultatet skrives til en **ny** fil, og der laves en kort rapport.

```bash
sudo docker exec oversaetter python -m oversaetter test \
  --en "/mnt/unionfs/Media/Movies/Film (2024)/Film.en.srt" \
  --da "/mnt/unionfs/Media/Movies/Film (2024)/Film.da.srt" \
  --out "/data/film-test.da.srt"
```

Resultatet ligger derefter i `/opt/oversaetter/data/film-test.da.srt` og rapporten i `/opt/oversaetter/data/film-test.da.srt.rapport.txt`. Rapporten viser antal fejl før og efter, antal rettelser og prisen. Prisen tæller med i månedens budget.

## Hvis noget går galt

- Mislykkes en oversættelse, prøves den igen ved næste runde. Går det galt igen, markeres den som "fejlet" og prøves ikke igen automatisk.
- Billedbaserede undertekster (PGS/VobSub) kan ikke oversættes og springes over.

## For udviklere

```bash
pip install -r requirements.txt pytest ruff
ruff check . && ruff format --check . && pytest
```

GitHub-handlingen "Tjek" kører det samme plus `docker build` ved hvert push.
