# EVE Retroindustry

A local industry calculator for EVE Online. Runs as a web app on your machine — blueprint cost analysis, bill of materials expansion, Jita market pricing, asset tracking, contract browsing, and production project management. Multi-character support: load all your alts and switch between them per page.

> **Note on the project.** I build this primarily for my own EVE career — features land when I need them, and priorities follow whatever I'm doing in-game. It's shared publicly as-is: if you find it useful, you're welcome to use it. There's no support commitment or roadmap promise, but bug reports and ideas are welcome via [Issues](https://github.com/ScoopEMPRetro/Eve-retroindustry/issues).

![Dashboard — multi-character overview](docs/screenshots/dashboard.png)

---

## Features

- **Multi-character Dashboard** — log in any number of alts via EVE SSO; see all characters at a glance with portrait, corporation, current docked location, the skill in training with a live countdown, asset count, and estimated net worth. A **Total available cash** tile sums wallet ISK across every character
- **Production Planner** — enter any ship or component, pick a station, get a full bill of materials with Jita buy/sell prices, your asset coverage, manufacturing job time and fees (EIV × SCI × facility tax × SCC), profit vs. market and vs. stock, and the cheapest make-vs-buy decomposition
- **Blueprint Library** — full character (and alt) blueprint list with ME/TE levels, BPO vs BPC, runs remaining, organised by station and container
- **Asset Tracking** — character + corporation inventory grouped by location and container (incl. all corp hangar divisions), with estimated ISK value per stack and per station

![Production Plan — Raven (ME 10 / TE 20)](docs/screenshots/production-plan.png)

- **Jita Price Cache** — fetches live market data from ESI, caches locally, refresh on demand; secondary trade hubs (Amarr / Dodixie / Rens / Hek) and any custom station/citadel can be pulled in for side-by-side price comparison

![Prices — Jita + secondary hubs, filtered to the Battleship group](docs/screenshots/prices.png)

- **Structure & Rig Modelling** — supports Raitaru / Azbel / Sotiyo / Athanor / Tatara with per-slot rig selection; ME/TE bonuses applied correctly with security multiplier (highsec 1.0× / lowsec 1.9× / null 2.1×)
- **Production Projects** — save a plan as a project, track which jobs are done, and get a unified shopping list across multi-stage manufacturing
- **Market Orders** — open buy/sell orders for every character and corporation, split into active vs. completed/expired

![Market Orders — active buy/sell across all characters](docs/screenshots/orders.png)

- **Industry Jobs** — running and finished manufacturing/reaction jobs, with per-character slot usage (used / available, derived from skills)

![Industry Jobs — running jobs with per-character slot usage](docs/screenshots/jobs.png)

- **Contracts** — browse your own **personal + corporation** contracts, plus a **public contract browser**: index a whole region once, then search it locally by item, type, or price (ESI exposes no contract search, so the region is fully indexed into a local cache). Public contract prices can be pulled straight into the Production Planner for a side-by-side profit comparison against market prices
- **Wallet** — personal and corporation wallet balances
- **In-app updates** — check for new releases and apply them without leaving the app
- **System tray** — runs in the system tray; right-click for **Open App** and **Quit**

![Assets — inventory across all characters and corporation hangars](docs/screenshots/assets.png)

---

## Installation

### Desktop (Windows / Linux)

1. Download the latest release from [**Releases**](https://github.com/ScoopEMPRetro/Eve-retroindustry/releases/latest)
2. Extract the ZIP anywhere (Linux also ships a single-file `.AppImage`)
3. Run `EVE_Retroindustry.exe` (Windows) or `EVE_Retroindustry` (Linux)
4. On first launch the app downloads ~5 MB of game data automatically
5. Open the system tray icon → **Open App**, then click **Log In** in the top right and authenticate with your EVE character. Add more alts by clicking **+ Add Character** in the character dropdown.

No Python, no dependencies, no installation wizard.

> **Note:** Windows may show a SmartScreen warning on first launch because the executable is unsigned. Click *More info → Run anyway*.

### Android (experimental)

An `EveRetroindustry.apk` is published with each release. It runs the full app on-device (a bundled Python server behind a native WebView). It's **arm64 only** and must be sideloaded:

1. Download `EveRetroindustry.apk` from the [latest release](https://github.com/ScoopEMPRetro/Eve-retroindustry/releases/latest)
2. Allow installation from unknown sources and install it manually
3. Later updates can be applied from inside the app (**About → Check for updates**)

This build is experimental — treat it as a work in progress rather than a polished release.

---

## Development Setup

Requires Python 3.11+.

```bash
git clone https://github.com/ScoopEMPRetro/Eve-retroindustry.git
cd Eve-retroindustry
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

Import the Static Data Export (SDE) into the local database:

```bash
python import_sde.py
```

Run the dev server:

```bash
uvicorn app.web.main:app --reload --port 8000
```

Open [http://localhost:8000](http://localhost:8000).

---

## Building a Release

Releases are built automatically by GitHub Actions when a version tag is pushed:

```bash
git tag v0.x.y && git push origin v0.x.y
```

The workflow builds Windows, Linux and Android binaries and creates a GitHub Release with:

- `EVE_Retroindustry-vX.Y.Z-win64.zip`
- `EVE_Retroindustry-vX.Y.Z-linux.zip` + `EVE_Retroindustry-vX.Y.Z-linux.AppImage`
- `EveRetroindustry.apk` (Android, arm64 sideload)
- `sde_base.db` (game data, downloaded by the app on first run)
- `version.json` (used by the in-app updater)

The Android `versionCode` is derived from the tag (e.g. `v0.8.33` → `833`), and the APK is signed with a release key stored in GitHub Secrets — so releases can be cut from any machine.

To build locally:

```bash
python scripts/build_sde_base.py
pyinstaller eve_retroindustry.spec --noconfirm
```

---

## Tech Stack

| Layer | Library |
|---|---|
| Web framework | FastAPI + Uvicorn |
| Templates | Jinja2 + Bootstrap 5 (dark) |
| Database | SQLite via sqlite3 |
| EVE API | ESI (esi.evetech.net) |
| HTTP client | httpx (async) |
| Tray icon | pystray + Pillow |
| Desktop shell | pywebview (PyQt6 / QtWebEngine) |
| Packaging | PyInstaller (onedir) |
| Android | Chaquopy (on-device CPython) + native WebView |

---

## Data & Privacy

All data is stored locally on your machine in:

| File | Contents |
|---|---|
| `eve_cache.db` | Blueprints, assets, prices, projects, OAuth tokens for all characters |
| `.eve_config.json` | EVE SSO client ID only |
| `eve_retroindustry.log` | Application log (frozen builds only) |

Nothing is sent to any third-party server other than the official EVE Online ESI API (`esi.evetech.net`) and the EVE SSO login server (`login.eveonline.com`).

---

## Support

I develop this in my spare time, primarily for my own EVE career, and share it publicly as-is. If it saves you ISK or time and you'd like to support continued development, you can buy me a coffee — entirely optional, and much appreciated:

[![Support me on Ko-fi](https://ko-fi.com/img/githubbutton_sm.svg)](https://ko-fi.com/retrovisor)

---

## Legal

EVE Online and the EVE logo are the registered trademarks of CCP hf. All rights are reserved worldwide. This application is not endorsed by or affiliated with CCP hf.

Market data and character information are fetched from the [EVE Swagger Interface (ESI)](https://esi.evetech.net) under CCP's developer license.

---

## License

MIT — see [LICENSE](LICENSE)
