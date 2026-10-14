# ComfyUI Launcher pre RunPod

Vlastný RunPod template: ComfyUI + launcher na porte 3000 + JupyterLab. Kto template vyberie a dá Deploy,
otvorí port 3000 a v prehliadači si klikne:

- **Workflows**: vyberie workflow, launcher mu nainštaluje workflow, custom nody a stiahne modely
- **Custom models**: vloží odkaz (Hugging Face, CivitAI, priamy https) + priečinok (napr. `loras`) + Hugging Face token
- **Custom nodes**: vloží GitHub odkaz, launcher nód naklonuje, doinštaluje závislosti a reštartuje ComfyUI na tlačidlo

| Port | Čo je |
|---|---|
| 3000 | Launcher (toto UI) |
| 8188 | ComfyUI |
| 8888 | JupyterLab (terminál, súbory) |

Je to napísané od nuly, nie je to kópia cudzieho obrazu. Názov v hlavičke zmeníš premennou `LAUNCHER_TITLE`.

## Čo je v priečinku

```
Dockerfile              obraz: Python 3.12, PyTorch (CUDA 12.8), ComfyUI v0.40.0, JupyterLab, launcher
start.sh                štart kontajnera (kopíruje ComfyUI na volume, SSH, Jupyter, launcher)
launcher/               aplikácia (FastAPI) + UI (static/) + katalóg workflowov (catalog/)
tests/                  193 automatických testov + ui_smoke.py (prechod UI v prehliadači)
.github/workflows/      GitHub Actions: spustí testy a postaví obraz do GitHub Container Registry
RUNPOD_TEMPLATE.md      presné hodnoty do RunPod template
```

## Postup od nuly po fungujúci template

**1. GitHub repo.** Na github.com vytvor nové repo (môže byť privátne), napr. `majklenzi-comfyui-launcher`.
Obsah tohto priečinka nahraj do koreňa repa (nie priečinok v priečinku):

```
git init -b main
git add -A
git commit -m "ComfyUI launcher"
git remote add origin https://github.com/<tvoje-meno>/majklenzi-comfyui-launcher.git
git push -u origin main
```

**2. Build.** Po pushi sa v záložke **Actions** spustí workflow. Najprv beží `test`, potom `build` (prvý build trvá
rádovo desiatky minút, obraz má niekoľko GB). Zelená fajka = obraz je v `ghcr.io/<tvoje-meno>/<repo>:latest`.
Ak sa niečo sfarbí na červeno, pošli mi text chyby z logu.

**3. Sprístupni balík.** GitHub robí nové balíky súkromné. RunPod ho bez prihlásenia nestiahne, takže:
tvoj profil → **Packages** → balík → **Package settings** → **Change visibility** → **Public**.

**4. RunPod template.** Manage → Templates → New Template, hodnoty sú v `RUNPOD_TEMPLATE.md`
(image, disky, porty `3000, 8188, 8888`, premenné).

**5. Skúšobný deploy.** Pri deployi nastav v **Additional filters** CUDA 12.8 alebo novšiu. Po štarte:
port 3000 → zadaj heslo → hore má svietiť "ComfyUI running" → klikni **Open ComfyUI**. Prvý štart trvá dlhšie,
ComfyUI sa kopíruje na volume.

## Heslo

Launcher inštaluje kód a sťahuje súbory, takže je chránený heslom vždy.

- Nastav `LAUNCHER_PASSWORD` v premenných template (aspoň 12 znakov), alebo
- nenastav nič a launcher si pri prvom štarte vygeneruje heslo. Nájdeš ho v **Logs** podu a v súbore
  `/workspace/.launcher/launcher_password.txt`.

Prihlasovacie meno je ľubovoľné. Heslo vypneš len výslovne cez `LAUNCHER_ALLOW_NO_AUTH=1`.

## Vlastný katalóg workflowov

Karty na stránke Workflows sú JSON súbory v `launcher/catalog/`. Formát a príklad je v `launcher/catalog/README.md`
a `_example.json`. Dve cesty:

- **v obraze:** pridaj `*.json` do `launcher/catalog/`, pushni, počkaj na build
- **bez nového buildu:** daj súbor do `/workspace/.launcher/catalog/` na volume (cez Jupyter), obnov stránku

Workflow `.json` dáš na GitHub (raw odkaz) alebo vedľa katalógu do `launcher/catalog/workflows/`.
Medzi modelmi uveď priame odkazy a priečinok. Nepribalil som žiadny cudzí workflow ani model, kým nepotvrdíš,
ktoré chceš a že ich môžeš šíriť. Je tam len karta **Essential nodes pack** (KJNodes, rgthree, Impact Pack + Subpack).

## Premenné prostredia

| Premenná | Predvolené | Význam |
|---|---|---|
| `LAUNCHER_PASSWORD` | vygeneruje sa | heslo do launchera |
| `LAUNCHER_ALLOW_NO_AUTH` | `0` | `1` = bez hesla (nie je odporúčané) |
| `LAUNCHER_TITLE` | ComfyUI Launcher | nadpis v UI |
| `JUPYTER_PASSWORD` | vygeneruje sa, vypíše do logov | token do Jupyteru |
| `HF_TOKEN`, `CIVITAI_TOKEN` | prázdne | tokeny pre celý pod (dajú sa zadať aj v UI) |
| `COMFY_ARGS` | prázdne | argumenty pre ComfyUI, napr. `--enable-manager` (pozri nižšie) |
| `AUTO_START_COMFY` | `1` | `0` = ComfyUI nespúšťať samo |
| `ENABLE_JUPYTER` | `1` | `0` = Jupyter nespúšťať |
| `PUBLIC_KEY` | nastaví RunPod | SSH kľúč, keď ho máš v účte |

## Bezpečnosť: čo je chránené a čo nie

- **Port 3000** má heslo (basic auth, po zlom hesle 1 s pauza), prísnu CSP, odmieta požiadavky z iných stránok.
- **Port 8188 (ComfyUI) nemá heslo**, rovnako ako každý štandardný ComfyUI template. Kto pozná odkaz podu, môže
  generovať na tvojej GPU. Preto je ComfyUI-Manager predvolene vypnutý: cez neho by ten istý človek mohol
  inštalovať kód. Ak ho chceš, daj `COMFY_ARGS=--enable-manager` a vedz, že ho tým otvoríš.
- **Port 8888 (Jupyter)** vždy žiada token.
- **Custom nody sú cudzí kód.** Launcher spúšťa `requirements.txt` a `install.py` ako ComfyUI-Manager. Inštaluj len repá, ktorým veríš.
  Torch a numpy (hlavná verzia) sú pri inštalácii zamknuté, aby node nerozbil CUDA PyTorch.
- **Token z Hugging Face** sa ukladá na volume do `/workspace/.launcher/settings.json` (práva 0600), UI ho nikdy
  nezobrazí a posiela sa len na `huggingface.co` (CivitAI kľúč len na `civitai.com`), nikdy na CDN, na ktoré
  odkaz presmeruje. **Ak volume zdieľaš alebo robíš jeho kópiu, klikni najprv v UI na Remove**, alebo používaj
  `HF_TOKEN` cez RunPod Secret.
- **Sťahovanie:** len `https` na porte 443, privátne a lokálne adresy sú zablokované (aj po presmerovaní),
  názvy súborov a priečinkov sa kontrolujú, skripty (`.py`, `.sh`…) sa do modelov nestiahnu. Známe obmedzenie:
  kontrola adresy prebehne tesne pred pripojením, takže útočník s vlastným DNS serverom by ju teoreticky mohol
  obísť. Odpoveď sa nikdy nevráti do UI (ukladá sa len do súboru) a bez hesla sa k tomu nikto nedostane.
- Balíky nainštalované za behu idú na volume (`/workspace/.pylibs`), takže prežijú reštart podu. Launcher a Jupyter
  ich nevidia (`python -s`), takže ich nemôžu rozbiť.

## Čo je otestované a čo nie

Otestované: 193 automatických testov (overovanie URL a ciest, sťahovanie s obnovením, tokeny pri presmerovaní,
inštalácia nódu z lokálneho git repa, API, heslo, CSRF, správa procesu ComfyUI cez náhradný server), prechod celého
UI v Chromiu so screenshotmi a nezávislá bezpečnostná kontrola kódu, ktorej nálezy sú opravené a majú testy.

**Neotestované, lebo na to tu nie je Docker ani RunPod:**
- samotný `docker build` (prvý build v Actions je zároveň prvý test; pošli mi log, ak spadne)
- beh na reálnom RunPod pode (porty, proxy odkazy, Jupyter pod proxy, SSH)
- reálne sťahovanie z Hugging Face a CivitAI (testy bežia proti lokálnemu serveru)
- reálny ComfyUI v0.40.0 pod launcherom (v testoch ho nahrádza falošný server) a inštalácia nódov z github.com
- PyTorch pre CUDA 12.8 sa v Dockerfile nepripína: berie sa najnovší, ktorý ten index ponúka. Ak raz pokazí
  build, pripni verziu v `Dockerfile`.

## Riešenie problémov

| Čo vidíš | Čo urobiť |
|---|---|
| "ComfyUI crashed" | Settings → Show ComfyUI log. Najčastejšie chýba závislosť custom nódu. |
| Stránka 3000 chce heslo a nepoznáš ho | Logs podu, alebo cez Jupyter súbor `/workspace/.launcher/launcher_password.txt` |
| Sťahovanie: "Access denied" | Gated model: ulož token a na stránke modelu na Hugging Face odsúhlas licenciu |
| Sťahovanie padlo v polovici | Zadaj ten istý odkaz znova, pokračuje tam, kde skončilo |
| Node nainštalovaný, ale nič sa nezmenilo | Tlačidlo **Restart ComfyUI** (nové modely reštart nepotrebujú, stačí `R` v ComfyUI) |
| Málo miesta | Zväčši volume pri deployi alebo zmaž modely na stránke Custom models |

## Aktualizácia

Verziu ComfyUI mení `ARG COMFYUI_REF` v `Dockerfile` (teraz `v0.40.0`). Po novom builde sa na existujúcom volume
ComfyUI nepreinštaluje, zostáva tá, ktorá je v `/workspace/ComfyUI`. Nový ComfyUI dostaneš na čistom volume.
