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
5. Fandt en gyldig søgning ikke dansk, og findes der en engelsk tekst, **oversætter programmet selv** med Claude (standard Opus 5.5, `TRANSLATE_MODE=direct`):
   - Den engelske tekst hentes: en ekstern `.en.srt` læses direkte, et indlejret engelsk tekstspor trækkes ud af videoen med ffmpeg (ikke SDH/forced, hvis der er flere). Billedbaserede spor (PGS/VobSub) springes over.
   - Teksten oversættes i bidder af 50 replikker med sammenhæng (som i testtilstanden `oversaet-test`). Afviser modellen en bid (refusal) også efter halvering, prøves den med `FALLBACK_MODEL` (standard Sonnet 5.5). Lykkes det heller ikke, beholder de replikker den engelske tekst, og det står tydeligt i loggen.
   - **Gratis kontrol** af hver replik (engelske rester, norske/svenske ord, kendte forkerte ord som "bestie" og "please", for lange linjer, manglende tal osv.). De flagede replikker (højst `MAX_FIX_LINES`, standard 60) rettes i **ét ekstra kald** med 2 nabolinjer på hver side.
   - Kreditlinjen sættes ind, og filen **uploades til Bazarr** (samme kald som Upload-knappen). Så registrerer Bazarr teksten, kører sync/Sub-Zero og kan senere udskifte den. Fejler uploaden, skriver programmet selv `<videonavn>.da.srt` ved siden af videoen og beder Bazarr om "scan disk".

   Er der heller ingen engelsk tekst, beder programmet Bazarr søge efter engelsk (se "Ingen engelsk tekst" nedenfor).
6. Husker i en lille database, hvad der er lavet, så intet bliver søgt eller oversat to gange.

Med `TRANSLATE_MODE=lingarr` kører det gamle flow: Bazarr oversætter med Lingarr og Claude Haiku, og programmet laver bagefter kontrol, korrektur med korrekturmodellen (`PROOFREAD_*`) og kreditlinje. Det er beholdt, så vi kan rulle tilbage.

Programmet ændrer ingen indstillinger i Bazarr, Lingarr, Radarr, Sonarr eller Seerr, sletter aldrig filer og rører kun den nye danske undertekstfil. Ud over at aflevere den danske tekst beder det kun Bazarr om tre ting: at søge efter dansk (eller engelsk, når der hverken er dansk eller engelsk) til et bestemt emne, at synkronisere med Radarr/Sonarr, når der er kommet noget nyt med mærket **oversaet**, som Bazarr ikke kender endnu (højst én gang pr. runde), og at scanne disken for ét emne, hvis uploaden fejlede.

Udgifterne til Claude har et loft (`MONTHLY_BUDGET_USD`, standard 5 dollars om måneden). I direct-tilstand betales hele oversættelsen herfra: når loftet er nået, startes der ingen nye oversættelser, før der er budget igen (en oversættelse, der er i gang, gøres færdig). I lingarr-tilstand gemmes Haikus oversættelse uden korrektur, når loftet er nået.

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
#    tmdb_api_key.txt er valgfri (filmkontekst fra TMDB, se "Bedre og hurtigere oversættelse").
sudo touch /opt/oversaetter/secrets/deepseek_api_key.txt /opt/oversaetter/secrets/gemini_api_key.txt \
  /opt/oversaetter/secrets/tmdb_api_key.txt
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
2026-10-17 20:15:40 INFO Gyldig søgning fandt ikke dansk - oversætter Filmnavn selv (Seerr) med claude-opus-5-5 (effort medium): 1412 replikker fra ekstern engelsk: Filmnavn.en.srt
2026-10-17 20:18:31 INFO Rettelsesrunde: 19 af 23 flagede replikker rettet
2026-10-17 20:18:33 INFO Uploadet til Bazarr: Filmnavn.da.srt (Bazarr registrerer og synkroniserer teksten)
2026-10-17 20:18:33 INFO Færdig: Filmnavn - model: claude-opus-5-5, replikker: 1412, flag: 23, rettelser: 19, flag efter: 4, beholdt engelsk: 0, fallback-model: 0, pris: 1.2800 USD, tid: 2 min 53 s, uploadet til Bazarr, kreditlinje indsat
```

Hver runde viser først Bazarrs egne tal og op til 5 eksempler på, hvorfor noget blev valgt eller fravalgt. Ser du

- `Bazarr melder ingen film eller afsnit, der mangler dansk` – tjek at dansk er med i sprogprofilen i Bazarr.
- `Bazarr svarede med HTTP 401` (eller Radarr/Sonarr/Seerr) – API-nøglen i `.env` er forkert.
- `Bazarr svarede ikke inden for ... (timeout)` eller `kunne ikke forbinde` – adressen i `.env` er forkert, eller tjenesten kører ikke.
- `venter: OpenSubtitles er sat på pause til ca. kl. 22:30` – Bazarr har sat en undertekst-tjeneste på pause (for mange kald, nede eller kvote brugt). Der søges og oversættes ikke, før alle aktiverede tjenester er tilbage. Andre grunde i loggen: `venter på at søgning bliver færdig` (Bazarr arbejder stadig på søgningen) og `søgningen tæller ikke` (en tjeneste blev sat på pause under søgningen).
- `OpenSubtitles har været sat på pause i over 24 timer - overvej at slå den fra i Bazarr` – tjenesten blokerer alle nye oversættelser. Slå den fra i Bazarr under Settings → Providers, eller ret årsagen (fx login eller kvote).

## Prøvetilstand (DRY_RUN)

Programmet starter i prøvetilstand (`DRY_RUN=true`). Så læser det kun (tjenestestatus, wanted-listen, Seerr og mærker) og skriver i loggen, hvad det **ville** gøre ("ville søge nu", "ville oversætte 1412 replikker med claude-opus-5-5", "ville bede Bazarr synkronisere") – det beder ikke Bazarr søge, synkronisere eller oversætte, kalder ikke Claude, uploader intet og skriver ingen filer. I direct-tilstand læses den engelske tekst dog (også med ffmpeg ved indlejrede spor), så antallet af replikker kan tælles.

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
- `--prompt v3|v4`: instruksen (standard v3).
- `--context on|off`: filmkontekst fra TMDB (kræver `--tmdb-id` og TMDB-nøglen; standard off).
- `--tmdb-id 12345`: TMDB-id til konteksten uden Radarr-opslag (`tv:12345` for en serie).
- `--glossary on|off`: ordliste pr. film før bidderne (standard off).
- `--parallel 4`: så mange bidder samtidig (standard 1).

Eksempel med alle forbedringer slået til:

```bash
sudo docker exec oversaetter python -m oversaetter oversaet-test \
  --en "/mnt/unionfs/Media/Movies/Film (2024)/Film.en.srt" --out "/data/film-v4.da.srt" \
  --provider anthropic --model claude-opus-5-5 \
  --prompt v4 --context on --tmdb-id 12345 --glossary on --parallel 4
```

Sådan virker den: filen oversættes i bidder af 50 replikker. Hver bid får de 5 foregående replikker med deres færdige danske oversættelse, de 3 næste engelske replikker og op til 30 tidligere par med navne og faste udtryk med som kontekst, så oversættelsen hænger sammen og bliver ensartet. Instruksen er Lingarr-prompten v3 (regel 1-15) tilpasset til bidder. Svarer modellen ikke med præcis de rigtige numre, prøves bidden én gang mere, derefter delt i to (25+25). Mangler der stadig replikker, beholder de den engelske tekst, og det står i loggen.

Bagefter køres den gratis kontrol (altid med de skarpere flag, så v3 og v4 kan sammenlignes), og rapporten skrives på skærmen (vil du gemme den, så tilføj `> /opt/oversaetter/data/rapport.txt` uden for `docker exec`-anførselstegnene). Rapporten viser model, provider, kontakterne, filmkontekst, ordlistens størrelse og pris, antal replikker og bidder (og fjernede replikker med kun fyldlyde), tokens ind/ud (og prompt-cachens læsninger/skrivninger), pris, tid, antal rettelsesflag fra kontrollen med eksempler, antal informationsflag (fx `reading_speed`, som ikke ville blive sendt til rettelse) og antal replikker, der faldt tilbage til engelsk. Priserne pr. model står i `MODEL_PRICES` i `oversaetter/config.py`; ukendte modeller giver "pris ukendt".

Nøgler:

| Provider | Fil på serveren |
|---|---|
| anthropic | `/opt/lingarr/secrets/anthropic_api_key.txt` (samme som driften) |
| deepseek | `/opt/oversaetter/secrets/deepseek_api_key.txt` |
| gemini | `/opt/oversaetter/secrets/gemini_api_key.txt` |
| TMDB (kontekst, valgfri) | `/opt/oversaetter/secrets/tmdb_api_key.txt` |

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

- **Korrekturmodel (kun TRANSLATE_MODE=lingarr): Claude Opus 5.5 med effort medium** (`PROOFREAD_MODEL=claude-opus-5-5`, `PROOFREAD_EFFORT=medium`, pris 4/20 USD pr. million tokens ind/ud). Valgt efter test på *Do Not Enter* (2026):

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

- **Oversættelse direkte med Opus 5.5, uden Lingarr og uden korrektur** (besluttet 18.10.2026). Fire modeller blev testet i én gennemgang på *Do Not Enter* med `oversaet-test`:

  | Model | Vurdering | Pris | Tid |
  |---|---|---|---|
  | **Opus 5.5 direkte** | 9/10 | 0,56 USD | 3 min |
  | DeepSeek V4 Pro | 8/10 | 0,35 USD | 25 min (pga. tænkning) |
  | Sonnet 5.5 | 7,5-8/10 | 0,28 USD | – |
  | Gemini 3.8 Flash | ubrugelig pga. kvote | – | – |

  Valgt: Opus 5.5 direkte, ingen korrektur af hele filen – kun en lille rettelsesrunde for de replikker, den gratis kontrol flager. Lingarr er taget ud af kæden. Forventet pris ca. 9 kr. pr. lang film mod ca. 14 kr. før (Haiku via Lingarr + Opus-korrektur).

  Indstillinger: `TRANSLATE_MODE=direct|lingarr` (standard `direct`; `lingarr` = det gamle flow, så vi kan rulle tilbage), `TRANSLATE_MODEL` (standard `claude-opus-5-5`), `TRANSLATE_EFFORT` (standard `medium`), `FALLBACK_MODEL` (standard `claude-sonnet-5-5`) og `MAX_FIX_LINES` (standard 60). `TRANSLATE_TIMEOUT_MINUTES` (standard 45) er den samlede tidsgrænse for ét emne; hvert kald til Claude har desuden en grænse på 10 minutter og ffmpeg på 15 minutter. `PROOFREAD_*`-indstillingerne bruges ikke i direct-tilstand (står de i `.env`, skrives én INFO-linje ved start). Tokens ind/ud registreres i budgettet med priserne for den model, der faktisk blev brugt (`MODEL_PRICES` i `oversaetter/config.py`).

  Aflevering til Bazarr 1.6.2: `POST /api/movies/subtitles?radarrid=<id>` for film og `POST /api/episodes/subtitles?seriesid=<id>&episodeid=<id>` for afsnit, multipart med `file`, `language=da`, `forced=false` og `hi=false` – præcis som Bazarrs Upload-knap. Bazarr lægger uploaden i sin jobkø, gemmer filen som `<videonavn>.da.srt` og kører sync/Sub-Zero. Programmet venter op til 3 minutter på, at filen er gemt. Ellers (eller ved en HTTP-fejl) skriver det selv filen og kalder `PATCH /api/movies?radarrid=<id>&action=scan-disk` (afsnit: `PATCH /api/series?seriesid=<id>&action=scan-disk`). Den færdige fil gemmes også i `/opt/oversaetter/data/direkte/`.
- **Testtilstand til modelsammenligning** (besluttet 18.10.2026): `oversaet-test` oversætter en hel engelsk fil i bidder med sammenhæng, uden Lingarr og uden korrektur. Formålet er at sammenligne modeller (Anthropic, DeepSeek og Gemini) i én gennemgang for at se, om vi kan droppe Lingarrs linje-for-linje-oversættelse og Opus-korrekturen. Beslutningen er taget (se ovenfor), og driften bruger nu samme oversættelse.

- **Bedre og hurtigere oversættelse** (besluttet 18.10.2026). Opus direkte giver 9/10 (*Just Play Dead*: 1.775 replikker, 9 min, 1,59 USD). Målet er 9,5 og under 3 min via:

  1. **Filmkontekst** (`CONTEXT=on`): titel, år, genre, aldersgrænse, originalsprog/land, handling (dansk hvis den findes) og op til 15 roller med køn i systeminstruksen. Kilder: Radarr/Sonarr (ingen ny nøgle) og, hvis `/opt/oversaetter/secrets/tmdb_api_key.txt` (eller `TMDB_API_KEY`) findes, TMDB (dansk handling, rolleliste med køn, for serier også afsnittets titel og handling). Fejler et opslag, oversættes der uden kontekst med en WARNING.
  2. **Ordliste pr. film** (`GLOSSARY=on`): ét kald før bidderne med hele den engelske tekst (højst 30.000 tokens, ellers de første 2/3) giver navne, kælenavne, steder, gentagne udtryk/slang med dansk oversættelse, tiltale (du/De) og tone – højst 60 termer. Ordlisten står i systeminstruksen for alle bidder (og caches), gemmes i databasen og genbruges i senere afsnit af samme serie. Prisen tæller med i budgettet.
  3. **Prompt v4** (`PROMPT_VERSION=v4`): v3 med prioritering ved modstrid (betydning, linjelængde, naturligt dansk, stil), "man" for generelt "you", sammensatte ord i ét ord, småt i ugedage/måneder/nationaliteter/sprog, anglicismer, fyldlyde udelades (replikker med kun fyldlyde fjernes fra filen), ensartet tiltale, fremmedsprog bevares og ordlisten bruges konsekvent. Hos DeepSeek/Gemini bedes modellen tænke først.
  4. **Skarpere kontrol** (med v4): nye flag `compound_space`, `generic_you`, `capitalized_weekday`, `english_word`, `reading_speed` (over `READING_SPEED_MAX` tegn/s, standard 25, og kun hvis den danske tekst ikke er kortere end den engelske – kun et informationsflag, se trin B2 nedenfor) og `inconsistent_term` (afviger fra ordlisten). `identical_to_english` flager ikke længere navne fra ordlisten, tal, enkelte ens ord (Stop, Okay ...) eller andre fremmedsprog. Flagede replikker går til rettelsesrunden som før.
  5. **Parallelle bidder** (`PARALLEL=4`): op til så mange bidder sendes samtidig, i rækkefølge og med samme retry-regler. De danske "foregående" linjer sendes kun, når de er færdige; ensartethed sikres af ordlisten. Tidsgrænsen gælder stadig, og fejl i én bid stopper ikke de andre.

  Søgereglerne, udvælgelsen, uploaden til Bazarr, kreditlinjen og budgettet er uændrede.

- **Trin B2: rettelser efter test14/test15** (18.10.2026). Prompt v4 gav ca. 9/10 på *Do Not Enter* både serielt (test14) og med `PARALLEL=4` (test15) – parallel kostede ikke kvalitet. Derfor:

  1. **`reading_speed` er kun et informationsflag**: grænsen er hævet til `READING_SPEED_MAX` (standard 25 tegn/s). Flaget tælles i rapporten og loggen ("informationsflag"), men sendes aldrig i rettelsesrunden – test14 gav 104 af 106 flag her, næsten alle på korte replikker, hvor det er filmens timing og ikke oversættelsen, der er problemet. Listen `INFO_FLAGS` i `oversaetter/checks.py` kan udvides med flere flag.
  2. **Flere engelske slangord flages** (`english_word`): fx bitch(es), dude, nobody/nobodies, crew, bossman, boss, Mr./Mrs., miss, sir, ma'am, buddy, guys, girl og "Man" som udbrud ("Man, det er koldt."). ", mand", ", man", "Man ved aldrig", cool, bro, nice, okay, awesome og vibe må stå. Prompt v4 har fået en fast oversættelsesliste (bitch → kælling, bitches → kællinger, dude → mand/makker, nobody/nobodies → et nul/nuller, crew → bande/hold, bossman → chefen, Mr./Mrs. → hr./fru, guys → folkens/venner, keep shooting → bliv ved med at skyde), "Oh my God"/"Jesus" → altid "Åh gud", og en regel om ja-svar på negative spørgsmål ("Definitely." → "Nej, slet ikke."/"Helt sikkert.", ikke "Bestemt ikke.").
  3. **Tomme fyldlyde uden genforsøg**: består den engelske replik kun af fyldlyde/interjektioner (uh, um, hmm, mm-hmm, oh, ah, huh, ugh, ooh …), må svaret være tomt – eller mangle – uden at bidden sendes igen. Replikken fjernes fra filen. Tomme svar på replikker med rigtige ord prøves igen som før.
  4. **Prompt-cache** (`PROMPT_CACHE=true`, standard): systeminstruksen (v4 + filmkontekst + ordliste) sendes som én blok med `cache_control: ephemeral`, så bid 2 og frem læser den fra Anthropics cache. Cache-læsninger koster 10 % og cache-skrivninger 125 % af inputprisen, og begge tælles med i prisen og budgettet. Bidderne i første runde af `PARALLEL=4` skriver hver deres cache (de starter samtidig); resten læser.

  **Anbefalede værdier (står i `.env.example`):** `PROMPT_VERSION=v4`, `CONTEXT=on`, `GLOSSARY=on`, `PARALLEL=4`, `READING_SPEED_MAX=25`, `PROMPT_CACHE=true`. Mangler `PROMPT_VERSION`, `CONTEXT`, `GLOSSARY` eller `PARALLEL` helt i `.env`, bruger programmet stadig de gamle værdier (v3, off, off, 1).

- **Trin B3: rettelser efter test16** (18.10.2026).

  1. **Ordlisten fritager kun egennavne**: ordliste-prompten kræver dansk oversættelse af engelske bandeord, skældsord, slang og tiltaleord (bitches → kællinger, dude → mand, holy shit → hold da kæft). `english_word` fritager kun ordlisteudtryk med samme engelske og danske form, der starter med stort bogstav ("The Paragon"). Nye ord: damn, gosh, jeez, seriously, literally, babe, sweetie, honey, bastard, asshole, jerk, loser(s), samt "holy shit", "hell!" og "what the hell" ("shit" alene må stadig stå).
  2. **Ordspil**: prompt v4 siger, at et ordspil, der ikke kan laves på dansk, oversættes ligeud. Nyt rettelsesflag `mixed_case` flager opfundne ord som "sundhedsTHC" (ikke THC, JD's, iPhone, eBay, sundheds-THC eller ord, der starter med stort, som OnlyFans og YouTube).
  3. **Flere faste oversættelser** i prompt v4: frequent-flyer miles → bonuspoint, "Who gives a shit?" → "Hvem fanden går op i det?", "Bye for now" → "Farvel så længe", og "alle sammen" i to ord.
  4. **`inconsistent_term` er mindre følsom**: en bøjning af ordlistens danske form tæller som den samme (de første 4 bogstaver, så regel/reglen/reglerne/regler er ens), og gentagelse i samme replik flages aldrig.

## Hvis noget går galt

- Mislykkes en oversættelse, prøves den igen ved næste runde. Går det galt igen, markeres den som "fejlet" og prøves ikke igen automatisk.
- Billedbaserede undertekster (PGS/VobSub) kan ikke oversættes og springes over. Mangler ffmpeg, eller fejler udtrækningen, prøves emnet igen i næste runde.
- Når en replik ender med at beholde den engelske tekst (afvist af begge modeller eller ugyldigt svar), står det som `ERROR ... beholder den engelske tekst` og `ADVARSEL: N af M replikker ... beholder den engelske tekst` i loggen.
- Fejler uploaden til Bazarr, står der `ADVARSEL: Upload til Bazarr fejlede` i loggen; filen er så skrevet ved siden af videoen, og Bazarr er bedt om at scanne disken.
- Bliver en oversættelse ikke færdig inden for `TRANSLATE_TIMEOUT_MINUTES`, gemmes intet, og emnet prøves igen i næste runde (direct).
- Alle kald til Anthropic, Bazarr, Radarr, Sonarr og Seerr har en fast tidsgrænse, så intet kan vente for evigt.
- (lingarr) Afviser korrekturmodellen en bid (refusal), prøves den én gang mere delt i to halvdele. Afvises en halvdel igen, eller giver en bid et ugyldigt svar eller timeout, springes den over (Haikus tekst beholdes), og der fortsættes med næste bid.
- (lingarr) Efterbehandlingen af ét emne må højst tage `POSTPROCESS_TIMEOUT_MINUTES` (standard 30) minutter. Derefter gemmes filen med de rettelser, der er nået, og loggen skriver en advarsel. Kreditlinjen sættes altid ind, og filen gemmes altid.
- Rammer hovedløkken en uventet fejl, logges hele fejlen, emnet markeres som "forsoeg_igen", og løkken fortsætter med næste runde.
- Ved opstart genoptages emner, der stod som "i_gang" (fx efter genstart), og loggen skriver hvilke.

## For udviklere

```bash
pip install -r requirements.txt pytest ruff
ruff check . && ruff format --check . && pytest
```

GitHub-handlingen "Tjek" kører det samme plus `docker build` ved hvert push.
