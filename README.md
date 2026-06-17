# B7 ID — hlídač startovného (watchdog)

Hlídá tržiště se startovným na [b7id.cz](https://b7id.cz) a **pošle ti e-mail**,
jakmile přibude nová nabídka. Běží v cloudu přes **GitHub Actions**, takže funguje
i s vypnutým počítačem.

## Jak to funguje
b7id.cz je sice React aplikace, ale nabídky se načítají z čistého JSON API:

```
GET https://app-main-prod.b7id.cz/market/listOffers?raceId=<raceId>  ->  {"offers": [...]}
```

Přihlášení je přes session cookie. Skript proto:
1. se přihlásí přihlašovacím formulářem (headless prohlížeč Playwright) → získá cookie,
2. ve stejném kontextu zavolá `listOffers` API,
3. porovná nabídky s uloženým stavem a na novou položku pošle e-mail.

> Pozn.: API samo v odpovědi vrací poznámku, že pro automatické dotazy je lepší volat
> tento endpoint než scrapovat frontend (je cachovaný a méně zatěžuje server). Skript
> přesně tohle dělá.

---

## Krok 1 — Vyzkoušení lokálně (volitelné, doporučené)

```bash
cd b7-watchdog
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python -m playwright install chromium

cp .env.example .env       # vyplň B7_EMAIL, B7_PASSWORD (+ SMTP, chceš-li i e-mail)
python watchdog.py         # MODE=watch: první běh uloží baseline a nic nepošle
```

První běh si jen uloží, co je na tržišti teď (`state/seen.json`), a **nepošle nic**.
Až příště přibude nová nabídka, dostaneš e-mail. Chceš-li vidět surovou odpověď API
(užitečné, až nějaké nabídky budou), spusť jednou `MODE=discovery python watchdog.py`
— uloží se do `debug/listOffers.json`.

---

## Krok 2 — Nasazení na GitHub Actions (běh 24/7)

1. Vytvoř na GitHubu **nový repozitář** a nahraj do něj obsah téhle složky.
   - **Doporučení: dej ho jako _public_.** U veřejných repozitářů jsou GitHub Actions
     minuty zdarma a neomezené, takže běh každých 5 minut nic nestojí. V kódu ani ve
     stavu nejsou žádné citlivé údaje (heslo a SMTP jsou v Secrets, ne v kódu).
   - Pokud chceš _private_ repo, hlídej limit Actions minut (free tier 2000 min/měs.) —
     pak zvol řidší interval (viz níže).
2. **Settings → Secrets and variables → Actions**.
3. Do **Secrets** (citlivé) přidej:
   - `B7_EMAIL`, `B7_PASSWORD` — přihlášení na b7id.cz
   - `SMTP_USER`, `SMTP_PASS` — odesílání e-mailu (u Gmailu je `SMTP_PASS` *App password*)
4. Do **Variables** (necitlivé) přidej:
   - `B7_RACE_ID` = `699b407072bb7f5cd634f41a`
   - `SMTP_HOST` (např. `smtp.gmail.com`), `SMTP_PORT` (`587`)
   - `MAIL_TO`, `MAIL_FROM`

Ručně workflow spustíš v **Actions → B7 marketplace watchdog → Run workflow**.

### Spolehlivé časování (externí časovač)
GitHub naplánované běhy (cron) spouští nespolehlivě (mezery i 3–6 h) a nepřetržitá
smyčka je proti smyslu jejich podmínek. Proto je workflow jen **krátký jednorázový běh**
a spouští ho **externí časovač zdarma (cron-job.org)** přes GitHub API každých ~5 min:

- URL: `https://api.github.com/repos/<owner>/<repo>/actions/workflows/watchdog.yml/dispatches`
- Metoda: `POST`, tělo: `{"ref":"main"}`
- Hlavičky: `Authorization: Bearer <PAT>`, `Accept: application/vnd.github+json`,
  `X-GitHub-Api-Version: 2022-11-28`

PAT je fine-grained token s oprávněním *Actions: Read and write* na tomto repu
(platnost max 1 rok – pak obnovit). `schedule` ve `watchdog.yml` (cca 4× denně) je jen
záloha, kdyby externí časovač vypadl.

### Gmail App password
Gmail nedovolí přihlášení běžným heslem. Zapni 2FA a vytvoř *App password*:
Google účet → Security → 2-Step Verification → App passwords. Ten 16místný kód dáš
do `SMTP_PASS`. (Seznam.cz apod. fungují obdobně, viz nastavení účtu.)

### Změna intervalu
Interval se řídí v **cron-job.org** (jak často volá GitHub API). Nastav tam třeba
každé 2 nebo 5 minut.

---

## Stav a první běh
- Stav (co už jsme viděli) se ukládá do `state/seen.json` a workflow ho po každém běhu
  commitne zpět do repa.
- **První běh** jen uloží baseline a nic nepošle (jinak by přišel e-mail o všem, co už
  na tržišti je). Upozornění chodí až na nově přidané nabídky.

## Omezení
- Smyčka kontroluje po 5 min – velmi rychle prodaný lístek (do pár minut) může uniknout.
  Lze zkrátit `INTERVAL`; API časté skenování samo vítá (vrací o tom poznámku).
- Pokud b7id.cz přidá CAPTCHA / dvoufaktor, bude potřeba úprava přihlášení.
