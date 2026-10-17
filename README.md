# stream365-oversaetter

Et lille program, der sørger for, at film og serier uden danske undertekster får en dansk oversættelse af høj kvalitet.

## Hvad gør programmet?

Hvert kvarter gør programmet dette:

1. Spørger Bazarr, hvilke film og afsnit der mangler danske undertekster.
2. Vælger kun:
   - film og serier/sæsoner, som nogen har ønsket via **Seerr** (programmet spørger Seerr direkte), og
   - film med mærket **oversaet** i Radarr og afsnit af serier med mærket **oversaet** i Sonarr.
3. Springer over, hvis der allerede findes danske undertekster (som fil eller inde i videoen).
4. Sørger for en **gyldig søgning**: beder Bazarr søge efter dansk til netop det emne og tjekker, at ingen af Bazarrs undertekst-tjenester var sat på pause lige før eller lige efter. Fandt søgningen dansk, er der intet at gøre.
5. Fandt en gyldig søgning ikke dansk, findes de engelske undertekster, og Bazarr bedes oversætte dem til dansk med det samme (Bazarr bruger Lingarr og Claude Haiku).
6. Når den danske fil er klar, bliver den tjekket:
   - **Gratis kontrol** af hver replik (engelske rester, norske/svenske ord, kendte forkerte ord som "bestie" og "please", for lange linjer, manglende tal osv.).
   - **Korrektur med korrekturmodellen** (standard Claude Opus), som kun retter rigtige fejl.
   - Bazarrs linje "Subtitles translated with ..." erstattes med **"Oversat af Claude for ≋"**.
7. Husker i en lille database, hvad der er lavet, så intet bliver søgt eller oversat to gange.

Programmet ændrer ingen indstillinger i Bazarr, Lingarr, Radarr, Sonarr eller Seerr, sletter aldrig filer og rører kun den nye danske undertekstfil. Ud over oversættelsen beder det kun Bazarr om to ting: at søge efter dansk til et bestemt emne, og at synkronisere med Radarr/Sonarr, når der er kommet noget nyt med mærket **oversaet**, som Bazarr ikke kender endnu (højst én gang pr. runde).

Udgifterne til korrekturen har et loft (standard 5 dollars om måneden). Når loftet er nået, bliver Haikus oversættelse gemt uden korrektur.

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
2026-10-17 20:15:00 INFO Søger nu efter dansk til Ny film (Seerr, ingen gyldig søgning endnu)
2026-10-17 20:15:40 INFO Bazarr melder 4 film og 9 afsnit på wanted-listen (heraf 4 film og 2 afsnit uden dansk)
2026-10-17 20:15:40 INFO Eksempel: Ny film - søger nu (Seerr)
2026-10-17 20:15:40 INFO Eksempel: Filmnavn - gyldig søgning fandt ikke dansk - oversætter (Seerr, ekstern engelsk: Filmnavn.en.srt)
2026-10-17 20:15:40 INFO Eksempel: Anden film - gyldig søgning fandt dansk - intet at gøre
2026-10-17 20:15:40 INFO Eksempel: Tredje film - venter: OpenSubtitles er sat på pause til ca. kl. 22:30
2026-10-17 20:15:40 INFO Eksempel: Serie 1x02 - ikke Seerr-ønske og mangler mærket oversaet
2026-10-17 20:15:40 INFO Runde: 6 mangler dansk, 1 oversættes, 1 søgt nu, 1 venter, 1 fandt dansk ved søgning, 1 ikke valgt, ...
2026-10-17 20:15:40 INFO Gyldig søgning fandt ikke dansk - beder Bazarr oversætte Filmnavn (Seerr, forsøg 1) fra ekstern engelsk: Filmnavn.en.srt
2026-10-17 20:31:12 INFO Færdig: Filmnavn - flag før: 23, flag efter: 2, rettelser fra korrektur: 41, korrekturmodel: claude-opus-5-5, pris: 0.3100 USD, ...
```

Hver runde viser først Bazarrs egne tal og op til 5 eksempler på, hvorfor noget blev valgt eller fravalgt. Ser du

- `Bazarr melder ingen film eller afsnit, der mangler dansk` – tjek at dansk er med i sprogprofilen i Bazarr.
- `Bazarr svarede med HTTP 401` (eller Radarr/Sonarr/Seerr) – API-nøglen i `.env` er forkert.
- `Bazarr svarede ikke inden for ... (timeout)` eller `kunne ikke forbinde` – adressen i `.env` er forkert, eller tjenesten kører ikke.
- `venter: OpenSubtitles er sat på pause til ca. kl. 22:30` – Bazarr har sat en undertekst-tjeneste på pause (for mange kald, nede eller kvote brugt). Der søges og oversættes ikke, før alle aktiverede tjenester er tilbage. Andre grunde i loggen: `venter på at søgning bliver færdig` (Bazarr arbejder stadig på søgningen) og `søgningen tæller ikke` (en tjeneste blev sat på pause under søgningen).
- `OpenSubtitles har været sat på pause i over 24 timer - overvej at slå den fra i Bazarr` – tjenesten blokerer alle nye oversættelser. Slå den fra i Bazarr under Settings → Providers, eller ret årsagen (fx login eller kvote).

## Prøvetilstand (DRY_RUN)

Programmet starter i prøvetilstand (`DRY_RUN=true`). Så læser det kun (tjenestestatus, wanted-listen, Seerr og mærker) og skriver i loggen, hvad det **ville** gøre ("ville søge nu", "ville oversætte nu", "ville bede Bazarr synkronisere") – det beder ikke Bazarr søge, synkronisere eller oversætte og skriver ingen filer.

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

Resultatet ligger derefter i `/opt/oversaetter/data/film-test.da.srt` og rapporten i `/opt/oversaetter/data/film-test.da.srt.rapport.txt`. Rapporten viser korrekturmodellen, antal fejl før og efter, antal rettelser fra korrektur og prisen. Prisen tæller med i månedens budget.

## Indstillinger og beslutninger

- **Hvornår der oversættes** (besluttet af Jesper 17.10.2026). Erstatter de gamle regler med ventetider på 30 min (Seerr), 48 t (mærket) og 24 t (første gang set):
  1. Kunder ønsker film og serier i Seerr. Seerr sætter kundens Plex-brugernavn som mærke i Radarr/Sonarr, men det mærke bruges ikke af programmet. Programmet spørger Seerr direkte, om noget er ønsket.
  2. Seerr-ønsker (film og serier/sæsoner): Bazarr søger først efter dansk tekst. Finder en gyldig søgning ingen dansk tekst, oversættes der straks.
  3. Film med Radarr-mærket **oversaet** og serier med Sonarr-mærket **oversaet**: samme regel. Der søges straks og oversættes straks, hvis en gyldig søgning ikke finder dansk. Ingen ventetid på 48 timer.
  4. Der oversættes **aldrig** uden en gyldig søgning. En søgning er kun gyldig, hvis ingen af Bazarrs aktiverede undertekst-tjenester var sat på pause (throttled, nede, kvote overskredet) hverken lige før eller lige efter søgningen.

  Har en tjeneste været sat på pause i over 24 timer i træk, skrives en advarsel i hver runde. Der oversættes stadig ikke. `SEERR_DELAY_MINUTES`, `TAG_DELAY_HOURS` og `FIRST_SEEN_DELAY_HOURS` bruges ikke længere. Står de i `.env`, ignoreres de (med én INFO-linje ved start).
- **Søgninger**: Programmet venter højst `SEARCH_TIMEOUT_MINUTES` (standard 10) på Bazarrs søgning. Bliver den ikke færdig, ses der på den igen i næste runde. Der startes højst `MAX_SEARCHES_PER_ROUND` (standard 10) nye søgninger pr. runde, så tjenesterne ikke bliver overbelastet, når mange emner kommer på én gang.

- **Korrekturmodel: Claude Opus 5.5 med effort medium** (`PROOFREAD_MODEL=claude-opus-5-5`, `PROOFREAD_EFFORT=medium`, pris 4/20 USD pr. million tokens ind/ud). Valgt efter test på *Do Not Enter* (2026):

  | Model | Rettelser | Pris | Vurdering |
  |---|---|---|---|
  | Sonnet, medium | 61 | 0,16 USD | ca. 8/10 |
  | Sonnet, high | – | – | ikke bedre end medium |
  | **Opus, medium** | 106 | 0,54 USD | ca. 8,5-9/10 |

  Opus rettede negationer, "either way", manglende småord og engelske ord, som Sonnet lod stå. Vil du spare, kan du sætte `PROOFREAD_MODEL=claude-sonnet-5-5` og priserne til 2/10.
- **Kreditlinje på to linjer** (valgt af Jesper 17.10.2026). Standard i `.env`:

  ```
  CREDIT_TEXT='Oversat til dig af\n<font color="#58D9AE">≋ </font><b>Stream<font color="#58D9AE">365</font></b>'
  ```

  `\n` (backslash + n) bliver til et linjeskift i undertekstfilen. Farven #58D9AE er mint fra Stream365-logoet (appens `--color-accent`). Er `CREDIT_TEXT` tom, sættes der ingen kreditlinje ind. Kreditlinjen bliver hverken flaget af kontrollen eller rettet af korrekturen.
- **Kendte forkerte ord**: Den gratis kontrol flager replikker med ord, der før er set forkert (bestie, bestien, menneskebestie, menneskebestien, uansett, please, sweetheart, prank, llige, here). Listen kan udvides med `KNOWN_BAD_WORDS` i `.env`. Låneordene okay, hey, wow, yo, cool, nice, shit, fuck og bro flages aldrig.

## Hvis noget går galt

- Mislykkes en oversættelse, prøves den igen ved næste runde. Går det galt igen, markeres den som "fejlet" og prøves ikke igen automatisk.
- Billedbaserede undertekster (PGS/VobSub) kan ikke oversættes og springes over.

## For udviklere

```bash
pip install -r requirements.txt pytest ruff
ruff check . && ruff format --check . && pytest
```

GitHub-handlingen "Tjek" kører det samme plus `docker build` ved hvert push.
