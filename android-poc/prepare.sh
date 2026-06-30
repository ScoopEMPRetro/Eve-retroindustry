#!/usr/bin/env bash
# Zkopíruje sdílený Python kód + data z rootu repa do Android gradle modulu.
# Volá se před `gradle assembleDebug` (v CI i lokálně). Cílové adresáře jsou
# gitignorované — jsou to build-time artefakty, ne zdroj.
#
#   app/  ──► app/src/main/python/app          (Chaquopy: `import app.web.main`)
#   app/web/templates/ ─► assets/bundle/app/web/templates  (Jinja čte z filesystému)
#   sde_base.db ───────► assets/bundle/sde_base.db          (SDE bootstrap)
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"

PY_DST="$HERE/app/src/main/python/app"
ASSET_DST="$HERE/app/src/main/assets/bundle"

echo "==> prepare: root=$ROOT"

# ── Python kód pro Chaquopy (bez __pycache__ a bez .db) ──────────────────────
rm -rf "$PY_DST"
mkdir -p "$PY_DST"
cp -r "$ROOT/app/." "$PY_DST/"
find "$PY_DST" -name '__pycache__' -type d -prune -exec rm -rf {} +
find "$PY_DST" -name '*.pyc' -delete

# ── Assety: templates + static + SDE (čtené z filesystému přes EVE_BUNDLE_DIR) ─
rm -rf "$ASSET_DST"
mkdir -p "$ASSET_DST/app/web"
cp -r "$ROOT/app/web/templates" "$ASSET_DST/app/web/templates"
cp -r "$ROOT/app/web/static" "$ASSET_DST/app/web/static"
cp "$ROOT/sde_base.db" "$ASSET_DST/sde_base.db"

echo "==> prepare: python -> $PY_DST"
echo "==> prepare: assets -> $ASSET_DST"
du -sh "$PY_DST" "$ASSET_DST" 2>/dev/null || true
echo "==> prepare: done"
