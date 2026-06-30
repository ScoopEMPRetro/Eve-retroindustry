"""
Chaquopy dependency proof-of-concept.

Cíl: ověřit, že se na Androidu naimportuje celý závislostní stack
EVE Retroindustry — hlavně nativně kompilovaný pydantic-core (Rust),
který je make-or-break. Vrací textový report, který MainActivity zobrazí.
"""
import io
import platform
import traceback


def _try_import(label: str, modname: str, version_attr: str = "__version__") -> str:
    try:
        mod = __import__(modname)
        # projdi tečkovou cestu
        for part in modname.split(".")[1:]:
            mod = getattr(mod, part)
        ver = getattr(mod, version_attr, "?")
        return f"  OK   {label:14s} {ver}"
    except Exception as exc:
        return f"  FAIL {label:14s} {type(exc).__name__}: {exc}"


def run_checks() -> str:
    out = io.StringIO()
    out.write("EVE Retroindustry — Chaquopy dependency PoC\n")
    out.write(f"Python {platform.python_version()} on {platform.machine()}\n")
    out.write("-" * 44 + "\n")

    checks = [
        ("fastapi", "fastapi"),
        ("starlette", "starlette"),
        ("pydantic", "pydantic"),
        ("pydantic_core", "pydantic_core"),   # ← Rust binary, klíčové
        ("sqlalchemy", "sqlalchemy"),
        ("httpx", "httpx"),
        ("httpcore", "httpcore"),
        ("h11", "h11"),
        ("anyio", "anyio"),
        ("jinja2", "jinja2"),
        ("jwt", "jwt"),
        ("multipart", "multipart"),
        ("uvicorn", "uvicorn"),
        ("sqlite3", "sqlite3"),
    ]
    for label, mod in checks:
        out.write(_try_import(label, mod) + "\n")

    # Ověř, že FastAPI + pydantic spolu reálně fungují (vytvoř model + app)
    out.write("-" * 44 + "\n")
    try:
        from fastapi import FastAPI
        from pydantic import BaseModel

        class Item(BaseModel):
            type_id: int
            name: str

        app = FastAPI()

        @app.get("/")
        def root() -> Item:
            return Item(type_id=34, name="Tritanium")

        # validace přes pydantic-core
        item = Item(type_id="34", name="Tritanium")  # coercion test
        assert item.type_id == 34
        out.write("  OK   FastAPI + pydantic model/validate works\n")
    except Exception:
        out.write("  FAIL FastAPI/pydantic runtime:\n")
        out.write(traceback.format_exc())

    # Ověř sqlite3 read/write (datová vrstva appky)
    try:
        import sqlite3
        c = sqlite3.connect(":memory:")
        c.execute("CREATE TABLE t (id INTEGER, name TEXT)")
        c.execute("INSERT INTO t VALUES (?,?)", (34, "Tritanium"))
        row = c.execute("SELECT name FROM t WHERE id=34").fetchone()
        assert row[0] == "Tritanium"
        out.write("  OK   sqlite3 read/write works\n")
    except Exception:
        out.write("  FAIL sqlite3 runtime:\n")
        out.write(traceback.format_exc())

    return out.getvalue()
