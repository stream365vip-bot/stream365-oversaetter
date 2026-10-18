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
5. Fandt en gyldig søgning ikke dansk, findes de engelske undertekster, og Bazarr bedes oversætte dem til dansk med det samme (Bazarr bruger Lingarr og Claude Haiku). Er der heller ingen engelsk tekst, beder programmet Bazarr søge efter engelsk (se "Ingen engelsk tekst" nedenfor).
6. Når den danske fil er klar, bliver den tjekket:
   - **Gratis kontrol** af hver replik (engelske rester, norske/svenske ord, kendte forkerte ord som "bestie" og "please", for lange linjer, manglende tal osv.).
   - **Korrektur med korrekturmodellen** (standard Claude Opus), som kun retter rigtige fejl.
   - Bazarrs linje "Subtitles translated with ..." erstattes med **"Oversat af Claude for ≋"**.
7. Husker i en lille database, hvad der er lavet, så intet bliver søgt eller oversat to gange.

Programmet ændrer ingen indstillinger i Bazarr, Lingarr, Radarr, Sonarr eller Seerr, sletter aldrig filer og rører kun den nye danske undertekstfil. Ud over oversættelsen beder det kun Bazarr om to ting: at søge efter dansk (eller engelsk, når der hverken er dansk eller engelsk) til et bestemt emne, og at synkronisere med Radarr/Sonarr, når der er kommet noget nyt med mærket **oversaet**, som Bazarr ikke kender endnu (højst én gang pr. runde).

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

# 3. Lav mappen til nøglerne til testtilstanden oversaet-test (DeepSeek og Gemini).
#    Filerne skal findes, før containeren startes - ellers laver Docker en mappe i stedet.
#    Tomme filer er i orden: så virker driften som normalt, og kun oversaet-test melder, at nøglen mangler.
sudo mkdir -p /opt/oversaetter/secrets
sudo touch /opt/oversaetter/secrets/deepseek_api_key.txt /opt/oversaetter/secrets/gemini_api_key.txt
sudo chown 1000:1000 /opt/oversaetter/secrets/*.txt
sudo chmod 600 /opt/oversaetter/secrets/*.txt

# 4. Lav din egen indstillingsfil
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

## Testtilstand: oversæt en hel fil

`oversaet-test` oversætter en hel engelsk undertekstfil direkte med en model – **uden** Lingarr og **uden** korrektur – så modeller kan sammenlignes i én gennemgang (se beslutningen 18.10.2026 nedenfor). Driften bliver ikke rørt: ingen database, intet budget, ingen kreditlinje, og der skrives kun til `--out`.

```bash
sudo docker exec oversaetter python -m oversaetter oversaet-test \
  --en "/mnt/unionfs/Media/Movies/Film (2024)/Film.en.srt" \
  --out "/data/film-deepseek.da.srt" \
  --provider deepseek --model deepseek-v4-pro
```

- `--provider`: `anthropic`, `deepseek` eller `gemini`.
- `--model`: fx `claude-sonnet-5-5`, `claude-opus-5-5`, `deepseek-v4-pro`, `deepseek-flash` eller `gemini-3.8-flash`.
- `--effort low|medium|high`: grundighed (Anthropic, standard medium; Gemini kun hvis angivet; bruges ikke hos DeepSeek).
- `--batch 50`: antal replikker pr. bid.

Sådan virker den: filen oversættes i bidder af 50 replikker. Hver bid får de 5 foregående replikker med deres færdige danske oversættelse, de 3 næste engelske replikker og op til 30 tidligere par med navne og faste udtryk med som kontekst, så oversættelsen hænger sammen og bliver ensartet. Instruksen er Lingarr-prompten v3 (regel 1-15) tilpasset til bidder. Svarer modellen ikke med præcis de rigtige numre, prøves bidden én gang mere, derefter delt i to (25+25). Mangler der stadig replikker, beholder de den engelske tekst, og det står i loggen.

Bagefter køres den gratis kontrol, og rapporten skrives på skærmen (vil du gemme den, så tilføj `> /opt/oversaetter/data/rapport.txt` uden for `docker exec`-anførselstegnene). Rapporten viser model, provider, antal replikker og bidder, tokens ind/ud, pris, tid, antal flag fra kontrollen med eksempler og antal replikker, der faldt tilbage til engelsk. Priserne pr. model står i `MODEL_PRICES` i `oversaetter/config.py`; ukendte modeller giver "pris ukendt".

Nøgler:

| Provider | Fil på serveren |
|---|---|
| anthropic | `/opt/lingarr/secrets/anthropic_api_key.txt` (samme som driften) |
| deepseek | `/opt/oversaetter/secrets/deepseek_api_key.txt` |
| gemini | `/opt/oversaetter/secrets/gemini_api_key.txt` |

Skriv nøglen i filen med `sudo nano` (kun nøglen, én linje). Mangler en nøgle, stopper kun testtilstanden med en dansk besked – driften kører videre.

## Indstillinger og beslutninger

- **Hvornår der oversættes** (besluttet af Jesper 17.10.2026). Erstatter de gamle regler med ventetider på 30 min (Seerr), 48 t (mærket) og 24 t (første gang set):
  1. Kunder ønsker film og serier i Seerr. Seerr sætter kundens Plex-brugernavn som mærke i Radarr/Sonarr, men det mærke bruges ikke af programmet. Programmet spørger Seerr direkte, om noget er ønsket.
  2. Seerr-ønsker (film og serier/sæsoner): Bazarr søger først efter dansk tekst. Finder en gyldig søgning ingen dansk tekst, oversættes der straks.
  3. Film med Radarr-mærket **oversaet** og serier med Sonarr-mærket **oversaet**: samme regel. Der søges straks og oversættes straks, hvis en gyldig søgning ikke finder dansk. Ingen ventetid på 48 timer.
  4. Der oversættes **aldrig** uden en gyldig søgning. En søgning er kun gyldig, hvis ingen af Bazarrs aktiverede undertekst-tjenester var sat på pause (throttled, nede, kvote overskredet) hverken lige før eller lige efter søgningen.

  Har en tjeneste været sat på pause i over 24 timer i træk, skrives en advarsel i hver runde. Der oversættes stadig ikke. `SEERR_DELAY_MINUTES`, `TAG_DELAY_HOURS` og `FIRST_SEEN_DELAY_HOURS` bruges ikke længere. Står de i `.env`, ignoreres de (med én INFO-linje ved start).
- **Ingen engelsk tekst** (besluttet af Jesper 17.10.2026):
  1. Som før: først en gyldig søgning efter dansk. Finder den dansk, er emnet færdigt.
  2. Finder den ikke dansk, og der er ingen brugbar engelsk tekstkilde (ekstern .srt eller indlejret tekstspor), beder programmet Bazarr søge efter **engelsk** (samme kald, `language=en`) med de samme regler om tjenester på pause før og efter søgningen.
  3. Findes der engelsk efter søgningen, oversættes der straks.
  4. Findes der stadig ikke engelsk, springes emnet over. Programmet tjekker dog hver runde (uden ny søgning), om der er dukket en engelsk tekst op, fx fordi Bazarr selv har hentet en, eller du har hentet en manuelt. Gør der det, oversættes der straks. En ny søgning efter engelsk startes tidligst 24 timer efter den forrige.

  I loggen ser det fx sådan ud: `Søger nu efter engelsk til X (ingen dansk og ingen engelsk kilde)` og `Engelsk fundet til X - oversætter`. Søgninger efter engelsk tæller med i `MAX_SEARCHES_PER_ROUND`. I prøvetilstand søges der heller ikke efter engelsk.
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

- **Testtilstand til modelsammenligning** (besluttet 18.10.2026): `oversaet-test` oversætter en hel engelsk fil i bidder med sammenhæng, uden Lingarr og uden korrektur. Formålet er at sammenligne modeller (Anthropic, DeepSeek og Gemini) i én gennemgang for at se, om vi kan droppe Lingarrs linje-for-linje-oversættelse og Opus-korrekturen. Driften er uændret, indtil en beslutning er taget.

## Hvis noget går galt

- Mislykkes en oversættelse, prøves den igen ved næste runde. Går det galt igen, markeres den som "fejlet" og prøves ikke igen automatisk.
- Billedbaserede undertekster (PGS/VobSub) kan ikke oversættes og springes over.
- Alle kald til Anthropic, Bazarr, Radarr, Sonarr og Seerr har en fast tidsgrænse, så intet kan vente for evigt.
- Afviser korrekturmodellen en bid (refusal), prøves den én gang mere delt i to halvdele. Afvises en halvdel igen, eller giver en bid et ugyldigt svar eller timeout, springes den over (Haikus tekst beholdes), og der fortsættes med næste bid.
- Efterbehandlingen af ét emne må højst tage `POSTPROCESS_TIMEOUT_MINUTES` (standard 30) minutter. Derefter gemmes filen med de rettelser, der er nået, og loggen skriver en advarsel. Kreditlinjen sættes altid ind, og filen gemmes altid.
- Rammer hovedløkken en uventet fejl, logges hele fejlen, emnet markeres som "forsoeg_igen", og løkken fortsætter med næste runde.
- Ved opstart genoptages emner, der stod som "i_gang" (fx efter genstart), og loggen skriver hvilke.

## For udviklere

```bash
pip install -r requirements.txt pytest ruff
ruff check . && ruff format --check . && pytest
```

GitHub-handlingen "Tjek" kører det samme plus `docker build` ved hvert push.
