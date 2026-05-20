# EVE Retroindustry

A local industry calculator for EVE Online. Runs as a web app on your machine — blueprint cost analysis, bill of materials expansion, Jita market pricing, asset tracking, and production project management.


---

## Features

- **Production Planner** — enter any ship or component, get a full bill of materials with Jita buy/sell prices and your asset coverage
- **Blueprint Library** — overview of all character blueprints with ME/TE levels, BPO vs BPC
- **Asset Tracking** — full character inventory sorted by location with estimated ISK value
- **Jita Price Cache** — fetches live market data from ESI, caches locally, refreshes on demand
- **Production Projects** — track multi-run manufacturing batches
- **EVE SSO Login** — OAuth2 PKCE, no password stored, works with any EVE character

---

## Installation (Windows / Linux)

1. Download the latest release from [**Releases**](https://github.com/ScoopEMPRetro/Eve-retroindustry/releases/latest)
2. Extract the ZIP anywhere
3. Run `EVE_Retroindustry.exe` (Windows) or `EVE_Retroindustry` (Linux)
4. On first launch the app downloads ~5 MB of game data automatically
5. Click **Log In** in the top right and authenticate with your EVE character

No Python, no dependencies, no installation wizard.

> **Note:** Windows may show a SmartScreen warning on first launch because the executable is unsigned. Click *More info → Run anyway*.

---

## Development Setup

Requires Python 3.11+.

```bash
git clone https://github.com/ScoopEMPRetro/Eve-retroindustry.git
cd Eve-retroindustry
python -m venv venv && source venv/bin/activate   # Windows: venv\Scripts\activate
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

The workflow builds Windows and Linux binaries and creates a GitHub Release with:
- `EVE_Retroindustry_windows.zip`
- `EVE_Retroindustry_linux.zip`
- `sde_base.db` (game data, downloaded by the app on first run)

To build locally:

```bash
# Linux
bash build.sh

# Windows
build.bat
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
| Packaging | PyInstaller (onedir) |

---

## Data & Privacy

All data is stored locally on your machine in:

| File | Contents |
|---|---|
| `eve_cache.db` | Blueprints, assets, prices, projects |
| `.eve_config.json` | OAuth tokens, character ID |

Nothing is sent to any third-party server other than the official EVE Online ESI API (`esi.evetech.net`) and the EVE SSO login server (`login.eveonline.com`).

---

## Legal

EVE Online and the EVE logo are the registered trademarks of CCP hf. All rights are reserved worldwide. This application is not endorsed by or affiliated with CCP hf.

Market data and character information are fetched from the [EVE Swagger Interface (ESI)](https://esi.evetech.net) under CCP's developer license.

---

## License

MIT — see [LICENSE](LICENSE)
