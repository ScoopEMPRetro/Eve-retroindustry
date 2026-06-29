"""
Industry jobs — běžící výrobní/reakční/výzkumné joby postavy.

ESI: GET /characters/{id}/industry/jobs/?include_completed=true
Scope: esi-industry.read_character_jobs.v1
"""
from __future__ import annotations
import httpx

ESI_BASE = "https://esi.evetech.net/latest"

ACTIVITY_LABELS: dict[int, str] = {
    1: "Manufacturing",
    3: "TE Research",
    4: "ME Research",
    5: "Copying",
    7: "Reverse Engineering",
    8: "Invention",
    9: "Reactions",
    11: "Reactions",
}


def activity_label(activity_id: int) -> str:
    return ACTIVITY_LABELS.get(activity_id, f"Activity {activity_id}")


async def fetch_industry_jobs(client: httpx.AsyncClient, char_id: int, token: str,
                              include_completed: bool = True) -> list[dict]:
    """Vrátí industry joby postavy. include_completed=true vrátí i ready/
    delivered z posledního období; aktivní filtrujeme až ve view."""
    try:
        r = await client.get(
            f"{ESI_BASE}/characters/{char_id}/industry/jobs/",
            params={"include_completed": str(include_completed).lower()},
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
            timeout=15,
        )
        if r.status_code == 200:
            return r.json()
    except Exception:
        pass
    return []
