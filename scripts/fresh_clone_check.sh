#!/usr/bin/env bash
# Fresh clone check for pam-pm. No credentials needed.
# Steps: pytest, wheel build, template check, pip install, pam version.
set -euo pipefail
cd "$(dirname "$0")/.."

unset META_API_KEY ADO_PAT SLACK_BOT_TOKEN SLACK_APP_TOKEN GITHUB_TOKEN || true

echo "1. pytest (no credentials)..."
python -m pytest -q

echo "2. wheel build..."
rm -rf dist build
if python -m build --version >/dev/null 2>&1; then
  python -m build --wheel --no-isolation -o dist
else
  python -m pip wheel . --no-deps -w dist
fi
WHEEL=$(ls dist/*.whl | head -n 1)
echo "built: $WHEEL"

echo "3. template check (templates and static inside wheel)..."
python - "$WHEEL" <<'EOF'
import sys
import zipfile
names = zipfile.ZipFile(sys.argv[1]).namelist()
need = ["web/templates/setup.html", "web/static/style.css"]
missing = [n for n in need if not any(x.endswith(n) for x in names)]
if missing:
    print("missing from wheel: " + ", ".join(missing))
    sys.exit(1)
print("templates and static files present in wheel")
EOF

echo "4. pip install from wheel..."
python -m pip install --force-reinstall --no-deps "$WHEEL"

echo "5. pam version..."
pam --version
pam-pm --version

echo "fresh clone check passed"
