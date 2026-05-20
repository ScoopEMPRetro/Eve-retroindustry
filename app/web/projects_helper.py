"""Helper funkce pro Production Projects."""
import sqlite3
import time
import json
from collections import defaultdict


def ensure_project_tables(conn: sqlite3.Connection) -> None:
    conn.execute("""CREATE TABLE IF NOT EXISTS production_projects (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        created_at REAL NOT NULL,
        updated_at REAL NOT NULL
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS project_plans (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        project_id INTEGER NOT NULL,
        product_type_id INTEGER NOT NULL,
        product_name TEXT NOT NULL,
        quantity INTEGER NOT NULL DEFAULT 1,
        me INTEGER DEFAULT 0,
        te INTEGER DEFAULT 0,
        station_name TEXT,
        facility_tax REAL DEFAULT 0,
        plan_json TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'pending',
        created_at REAL NOT NULL
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS project_shopping (
        project_id INTEGER NOT NULL,
        type_id INTEGER NOT NULL,
        name TEXT NOT NULL,
        needed INTEGER NOT NULL DEFAULT 0,
        purchased INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY(project_id, type_id)
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS project_jobs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        plan_id INTEGER NOT NULL,
        project_id INTEGER NOT NULL,
        type_id INTEGER NOT NULL,
        name TEXT NOT NULL,
        quantity INTEGER NOT NULL DEFAULT 1,
        runs INTEGER NOT NULL DEFAULT 1,
        step INTEGER NOT NULL DEFAULT 1,
        activity TEXT NOT NULL DEFAULT 'manufacturing',
        status TEXT NOT NULL DEFAULT 'pending'
    )""")
    conn.commit()


def list_projects(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute("""
        SELECT p.id, p.name, p.created_at, p.updated_at,
               COUNT(DISTINCT pl.id) AS plan_count,
               SUM(CASE WHEN pl.status='completed' THEN 1 ELSE 0 END) AS completed_plans,
               COUNT(DISTINCT ps.type_id) AS shopping_total,
               SUM(CASE WHEN ps.purchased >= ps.needed AND ps.needed > 0 THEN 1 ELSE 0 END) AS shopping_done
        FROM production_projects p
        LEFT JOIN project_plans pl ON pl.project_id = p.id
        LEFT JOIN project_shopping ps ON ps.project_id = p.id
        GROUP BY p.id ORDER BY p.updated_at DESC
    """).fetchall()
    return [
        {
            "id": r[0], "name": r[1], "created_at": r[2], "updated_at": r[3],
            "plan_count": r[4] or 0, "completed_plans": r[5] or 0,
            "shopping_total": r[6] or 0, "shopping_done": r[7] or 0,
        }
        for r in rows
    ]


def create_project(conn: sqlite3.Connection, name: str) -> int:
    now = time.time()
    cur = conn.execute(
        "INSERT INTO production_projects (name,created_at,updated_at) VALUES (?,?,?)",
        (name, now, now),
    )
    conn.commit()
    return cur.lastrowid


def add_plan_to_project(
    conn: sqlite3.Connection,
    project_id: int,
    plan_data: dict,
    station_name: str,
    facility_tax: float,
) -> int:
    now = time.time()
    bp = plan_data.get("blueprint") or {}
    cur = conn.execute(
        """
        INSERT INTO project_plans
        (project_id,product_type_id,product_name,quantity,me,te,station_name,facility_tax,plan_json,status,created_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            project_id,
            plan_data["product_type_id"],
            plan_data["product_name"],
            plan_data["quantity"],
            bp.get("me", 0),
            bp.get("te", 0),
            station_name,
            facility_tax,
            json.dumps(plan_data, default=str),
            "pending",
            now,
        ),
    )
    plan_id = cur.lastrowid

    for mat in plan_data.get("materials", []):
        missing = mat.get("missing") or 0
        if missing > 0:
            conn.execute(
                """
                INSERT INTO project_shopping (project_id,type_id,name,needed,purchased) VALUES (?,?,?,?,0)
                ON CONFLICT(project_id,type_id) DO UPDATE SET needed=needed+excluded.needed, name=excluded.name
                """,
                (project_id, mat["type_id"], mat["name"], missing),
            )

    for step_data in plan_data.get("manufacturing_steps", []):
        for job in step_data.get("jobs", []):
            conn.execute(
                """
                INSERT INTO project_jobs (plan_id,project_id,type_id,name,quantity,runs,step,activity,status)
                VALUES (?,?,?,?,?,?,?,?,?)
                """,
                (
                    plan_id,
                    project_id,
                    job["type_id"],
                    job["name"],
                    job.get("quantity", 1),
                    job.get("runs", 1),
                    step_data["step"],
                    job.get("activity", "manufacturing"),
                    "pending",
                ),
            )

    conn.execute(
        "UPDATE production_projects SET updated_at=? WHERE id=?", (now, project_id)
    )
    conn.commit()
    return plan_id


def get_project_detail(conn: sqlite3.Connection, project_id: int) -> dict | None:
    proj = conn.execute(
        "SELECT id,name,created_at,updated_at FROM production_projects WHERE id=?",
        (project_id,),
    ).fetchone()
    if not proj:
        return None

    plans = [
        {
            "id": r[0], "product_type_id": r[1], "product_name": r[2],
            "quantity": r[3], "me": r[4], "te": r[5],
            "station_name": r[6], "facility_tax": r[7], "status": r[8], "created_at": r[9],
        }
        for r in conn.execute(
            """
            SELECT id,product_type_id,product_name,quantity,me,te,station_name,facility_tax,status,created_at
            FROM project_plans WHERE project_id=? ORDER BY created_at
            """,
            (project_id,),
        ).fetchall()
    ]

    shopping = [
        {"type_id": r[0], "name": r[1], "needed": r[2], "purchased": r[3]}
        for r in conn.execute(
            "SELECT type_id,name,needed,purchased FROM project_shopping WHERE project_id=? ORDER BY name",
            (project_id,),
        ).fetchall()
    ]

    # Načti vstupy každého jobu z uloženého plan_json (agreguj přes plány)
    # (step, type_id) -> {input_type_id: {name, quantity, is_leaf, activity}}
    plan_input_map: dict = {}
    for plan_id_row, plan_json_str in conn.execute(
        "SELECT id, plan_json FROM project_plans WHERE project_id=?", (project_id,)
    ).fetchall():
        try:
            pd = json.loads(plan_json_str)
        except Exception:
            continue
        for step_data in pd.get("manufacturing_steps", []):
            sn = step_data["step"]
            for job in step_data.get("jobs", []):
                key = (sn, job["type_id"])
                if key not in plan_input_map:
                    plan_input_map[key] = {}
                for inp in job.get("inputs", []):
                    tid = inp["type_id"]
                    if tid not in plan_input_map[key]:
                        plan_input_map[key][tid] = {
                            "type_id": tid,
                            "name": inp["name"],
                            "quantity": inp.get("quantity", 0),
                            "is_leaf": inp.get("is_leaf", True),
                            "activity": inp.get("activity", ""),
                        }
                    else:
                        plan_input_map[key][tid]["quantity"] += inp.get("quantity", 0)

    # Jobs grouped by step, then merged by type_id within step
    jobs_raw = conn.execute(
        """
        SELECT id,plan_id,type_id,name,quantity,runs,step,activity,status
        FROM project_jobs WHERE project_id=? ORDER BY step,name
        """,
        (project_id,),
    ).fetchall()

    # Merge jobs with same type_id+step
    merged: dict = {}  # (step, type_id) -> job dict
    for r in jobs_raw:
        jd = {
            "id": r[0], "plan_id": r[1], "type_id": r[2], "name": r[3],
            "quantity": r[4], "runs": r[5], "step": r[6], "activity": r[7], "status": r[8],
        }
        key = (jd["step"], jd["type_id"])
        if key not in merged:
            merged[key] = {**jd, "job_ids": [jd["id"]], "completed": jd["status"] == "completed"}
        else:
            merged[key]["quantity"] += jd["quantity"]
            merged[key]["runs"] += jd["runs"]
            merged[key]["job_ids"].append(jd["id"])
            if jd["status"] != "completed":
                merged[key]["completed"] = False

    # Přidej inputs ke každému merged jobu
    for key, job in merged.items():
        inputs = plan_input_map.get(key, {})
        job["inputs"] = sorted(inputs.values(), key=lambda x: x["name"])

    steps_map: dict = defaultdict(list)
    for key, job in merged.items():
        steps_map[key[0]].append(job)

    steps = []
    for step_num in sorted(steps_map.keys()):
        step_jobs = sorted(steps_map[step_num], key=lambda j: j["name"])
        steps.append({
            "step": step_num,
            "jobs": step_jobs,
            "all_done": all(j["completed"] for j in step_jobs),
        })

    total_jobs = sum(len(s["jobs"]) for s in steps)
    done_jobs = sum(1 for s in steps for j in s["jobs"] if j["completed"])

    return {
        "id": proj[0], "name": proj[1], "created_at": proj[2], "updated_at": proj[3],
        "plans": plans, "shopping": shopping, "steps": steps,
        "total_jobs": total_jobs, "done_jobs": done_jobs,
        "shopping_done": sum(
            1 for s in shopping if s["purchased"] >= s["needed"] and s["needed"] > 0
        ),
    }
