"""
Wallet — ISK balance, journal a market transactions pro postavu i korporaci.

ESI endpointy:
  Postava:
    GET /characters/{id}/wallet/                 → float (balance)
    GET /characters/{id}/wallet/journal/         → list (paginated, ~2500/strana)
    GET /characters/{id}/wallet/transactions/    → list (max 2500, from_id paging)
  Korporace (vyžaduje Accountant / Junior Accountant role → jinak 403):
    GET /corporations/{id}/wallets/              → [{division, balance}]
    GET /corporations/{id}/wallets/{div}/journal/
    GET /corporations/{id}/wallets/{div}/transactions/

Scopy: esi-wallet.read_character_wallet.v1, esi-wallet.read_corporation_wallets.v1
(role v korporaci: esi-characters? — ne, jen wallet scope + in-game role).
"""
from __future__ import annotations
import httpx

ESI_BASE = "https://esi.evetech.net/latest"


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}", "Accept": "application/json"}


async def fetch_balance(client: httpx.AsyncClient, char_id: int, token: str) -> float | None:
    try:
        r = await client.get(f"{ESI_BASE}/characters/{char_id}/wallet/",
                             headers=_auth(token), timeout=10)
        if r.status_code == 200:
            return float(r.json())
    except Exception:
        pass
    return None


async def fetch_journal(client: httpx.AsyncClient, char_id: int, token: str,
                        pages: int = 1) -> list[dict]:
    """Wallet journal — nejnovější transakce první. Stáhne `pages` stránek."""
    out: list[dict] = []
    for page in range(1, pages + 1):
        try:
            r = await client.get(
                f"{ESI_BASE}/characters/{char_id}/wallet/journal/",
                params={"page": page}, headers=_auth(token), timeout=15,
            )
        except Exception:
            break
        if r.status_code != 200:
            break
        batch = r.json()
        if not batch:
            break
        out.extend(batch)
        total_pages = int(r.headers.get("x-pages", 1))
        if page >= total_pages:
            break
    return out


async def fetch_transactions(client: httpx.AsyncClient, char_id: int, token: str) -> list[dict]:
    """Market transakce postavy (ESI vrací posledních ~2500)."""
    try:
        r = await client.get(
            f"{ESI_BASE}/characters/{char_id}/wallet/transactions/",
            headers=_auth(token), timeout=15,
        )
        if r.status_code == 200:
            return r.json()
    except Exception:
        pass
    return []


# ── Korporace ───────────────────────────────────────────────────────────────

async def fetch_corp_wallets(client: httpx.AsyncClient, corp_id: int, token: str
                             ) -> tuple[list[dict] | None, str | None]:
    """Vrátí ([{division, balance}], None) nebo (None, error_message).
    403 = postava nemá roli Accountant/Junior Accountant.
    """
    try:
        r = await client.get(f"{ESI_BASE}/corporations/{corp_id}/wallets/",
                             headers=_auth(token), timeout=12)
        if r.status_code == 200:
            return r.json(), None
        if r.status_code == 403:
            return None, "Tato postava nemá roli Accountant / Junior Accountant pro čtení korporátní peněženky."
        return None, f"ESI vrátilo HTTP {r.status_code}."
    except Exception as exc:
        return None, str(exc)


async def fetch_corp_journal(client: httpx.AsyncClient, corp_id: int, division: int,
                             token: str, pages: int = 1) -> list[dict]:
    out: list[dict] = []
    for page in range(1, pages + 1):
        try:
            r = await client.get(
                f"{ESI_BASE}/corporations/{corp_id}/wallets/{division}/journal/",
                params={"page": page}, headers=_auth(token), timeout=15,
            )
        except Exception:
            break
        if r.status_code != 200:
            break
        batch = r.json()
        if not batch:
            break
        out.extend(batch)
        if page >= int(r.headers.get("x-pages", 1)):
            break
    return out


async def fetch_corp_transactions(client: httpx.AsyncClient, corp_id: int, division: int,
                                  token: str) -> list[dict]:
    try:
        r = await client.get(
            f"{ESI_BASE}/corporations/{corp_id}/wallets/{division}/transactions/",
            headers=_auth(token), timeout=15,
        )
        if r.status_code == 200:
            return r.json()
    except Exception:
        pass
    return []


# ── Humanizace ref_type ──────────────────────────────────────────────────────

_REF_TYPE_LABELS: dict[str, str] = {
    "player_trading": "Player Trading",
    "market_transaction": "Market Transaction",
    "market_escrow": "Market Escrow",
    "transaction_tax": "Transaction Tax",
    "brokers_fee": "Broker's Fee",
    "bounty_prizes": "Bounty Prizes",
    "agent_mission_reward": "Mission Reward",
    "agent_mission_time_bonus_reward": "Mission Time Bonus",
    "corporation_account_withdrawal": "Corp Withdrawal",
    "industry_job_tax": "Industry Job Tax",
    "manufacturing": "Manufacturing",
    "contract_price": "Contract Price",
    "contract_reward": "Contract Reward",
    "contract_collateral": "Contract Collateral",
    "contract_brokers_fee": "Contract Broker's Fee",
    "contract_deposit": "Contract Deposit",
    "insurance": "Insurance",
    "player_donation": "Player Donation",
    "corporate_reward_payout": "Corp Reward Payout",
    "asset_safety_recovery_tax": "Asset Safety Tax",
    "structure_gate_jump": "Structure Gate Jump",
    "reprocessing_tax": "Reprocessing Tax",
    "jump_clone_activation_fee": "Jump Clone Fee",
    "jump_clone_installation_fee": "Jump Clone Install",
    "skill_purchase": "Skill Purchase",
    "war_fee": "War Fee",
    "office_rental_fee": "Office Rental",
    "factory_slot_rental_fee": "Factory Slot Rental",
    "market_provider_tax": "Market Provider Tax",
    "ess_escrow_transfer": "ESS Escrow Transfer",
}


def humanize_ref_type(ref_type: str) -> str:
    if ref_type in _REF_TYPE_LABELS:
        return _REF_TYPE_LABELS[ref_type]
    return ref_type.replace("_", " ").title()
