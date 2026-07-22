"""Static cockpit build and delivery boundaries."""

import json
from pathlib import Path


ROOT = Path(__file__).parents[1]
WEB_ROOT = ROOT / "src/hindsight/web"


def test_frontend_build_is_typed_static_and_reproducible_by_ci():
    package = json.loads((ROOT / "package.json").read_text())
    workflow = (ROOT / ".github/workflows/ci.yml").read_text()

    assert package["scripts"]["build:web"].startswith("vite build")
    assert package["scripts"]["check:web"].startswith("tsc ")
    assert package["scripts"]["test:web"].startswith("vitest run")
    assert "npm ci" in workflow
    assert "npm run check:web" in workflow
    assert "npm run test:web" in workflow
    assert "npm run build:web" in workflow
    assert "git diff --exit-code -- src/hindsight/web" in workflow


def test_static_bundle_has_stable_entrypoints_and_a_bounded_payload():
    index = (WEB_ROOT / "index.html").read_text()
    script = WEB_ROOT / "assets/app.js"
    styles = WEB_ROOT / "assets/styles.css"

    assert '<div id="root"></div>' in index
    assert '<script src="/config.js"></script>' in index
    assert 'src="/assets/app.js"' in index
    assert 'href="/assets/styles.css"' in index
    assert script.stat().st_size < 350_000
    assert styles.stat().st_size < 50_000


def test_local_api_lambda_and_s3_use_the_same_static_output():
    api = (ROOT / "src/hindsight/api.py").read_text()
    dashboard = (ROOT / "src/hindsight/dashboard.py").read_text()
    builder = (ROOT / "scripts/build_lambda_artifacts.py").read_text()
    terraform = (ROOT / "infra/terraform/app/main.tf").read_text()

    assert 'app.mount("/", StaticFiles(directory=WEB_ROOT, html=True)' in api
    assert 'assets.joinpath("app.js")' in dashboard
    assert 'assets.joinpath("styles.css")' in dashboard
    assert '"web",' in builder
    assert 'web_root     = "${path.module}/../../../src/hindsight/web"' in terraform
    assert 'fileset(local.web_root, "**")' in terraform
