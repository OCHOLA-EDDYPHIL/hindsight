"""Serve the private no-feedback owner review on the loopback interface."""

from __future__ import annotations

import argparse
import html
import json
import pathlib
import secrets
import sys
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from hindsight.v4_corpus import record_review_decision  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--packet", type=pathlib.Path, required=True)
    parser.add_argument("--state", type=pathlib.Path, required=True)
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    packet = _load_object(args.packet)
    state = _load_object(args.state)
    csrf = secrets.token_urlsafe(32)

    class ReviewHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            if self.path != "/":
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            current = _load_object(args.state)
            body = _render(packet=packet, state=current, csrf=csrf).encode()
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'",
            )
            self.send_header("Referrer-Policy", "no-referrer")
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self) -> None:  # noqa: N802
            if self.path != "/decision":
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            if self.headers.get("Host") not in {
                f"127.0.0.1:{args.port}",
                f"localhost:{args.port}",
            }:
                self.send_error(HTTPStatus.FORBIDDEN)
                return
            try:
                length = int(self.headers.get("Content-Length") or "0")
            except ValueError:
                self.send_error(HTTPStatus.BAD_REQUEST)
                return
            if length <= 0 or length > 4096:
                self.send_error(HTTPStatus.BAD_REQUEST)
                return
            form = parse_qs(self.rfile.read(length).decode(), strict_parsing=True)
            try:
                if form["csrf"] != [csrf]:
                    raise ValueError("invalid review request")
                current = _load_object(args.state)
                updated = record_review_decision(
                    packet=packet,
                    state=current,
                    index=int(form["index"][0]),
                    choice=form["choice"][0],
                    ambiguous={"yes": True, "no": False}[form["ambiguous"][0]],
                )
            except (KeyError, ValueError, IndexError):
                self.send_error(HTTPStatus.BAD_REQUEST)
                return
            _atomic_write(args.state, updated)
            self.send_response(HTTPStatus.SEE_OTHER)
            self.send_header("Location", "/")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()

        def log_message(self, format: str, *args: object) -> None:
            return

    if state.get("pool_sha256") != packet.get("pool_sha256"):
        raise ValueError("review state differs from its packet")
    server = ThreadingHTTPServer(("127.0.0.1", args.port), ReviewHandler)
    print(f"Private review available at http://127.0.0.1:{args.port}/")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


def _render(*, packet: dict, state: dict, csrf: str) -> str:
    completed = len(state["decisions"])
    total = len(packet["items"])
    if completed == total:
        content = """
          <section class="complete" aria-labelledby="complete-title">
            <p class="context">Review recorded</p>
            <h1 id="complete-title">All items are complete.</h1>
            <p>Close this browser window, then finalize the review from the private workspace.</p>
          </section>
        """
    else:
        item = packet["items"][completed]
        choices = "".join(
            f"""
            <label class="choice">
              <input type="radio" name="choice" value="{html.escape(row["choice"])}" required>
              <span class="letter">{chr(65 + index)}</span>
              <span>{html.escape(row["text"])}</span>
            </label>
            """
            for index, row in enumerate(item["choices"])
        )
        content = f"""
          <header class="review-heading">
            <p class="context">Private clarity review</p>
            <p class="progress" aria-label="Item {completed + 1} of {total}">{completed + 1} / {total}</p>
          </header>
          <main id="main">
            <section class="scenario" aria-labelledby="scenario-title">
              <h1 id="scenario-title">What is the clearest first response?</h1>
              <p>{html.escape(item["scenario"])}</p>
            </section>
            <form method="post" action="/decision">
              <input type="hidden" name="csrf" value="{html.escape(csrf)}">
              <input type="hidden" name="index" value="{completed + 1}">
              <fieldset class="choices">
                <legend>Choose one answer</legend>
                {choices}
              </fieldset>
              <fieldset class="ambiguity">
                <legend>Are two or more answers reasonably defensible?</legend>
                <label><input type="radio" name="ambiguous" value="yes" required> Yes</label>
                <label><input type="radio" name="ambiguous" value="no" required> No</label>
              </fieldset>
              <button type="submit">Record and continue</button>
              <p class="notice">Answers cannot be changed. Correctness is not shown during review.</p>
            </form>
          </main>
        """
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Private corpus review</title>
  <style>
    :root {{
      color-scheme: light dark;
      --bg: #f4f6f3;
      --surface: #e8ece7;
      --text: #17201b;
      --muted: #526057;
      --line: #aab4ad;
      --accent: #285c3e;
      --accent-text: #f4f7f4;
      font-family: ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    }}
    @media (prefers-color-scheme: dark) {{
      :root {{
        --bg: #101512;
        --surface: #18201b;
        --text: #edf2ee;
        --muted: #a5b0a9;
        --line: #465149;
        --accent: #b7ec57;
        --accent-text: #13200f;
      }}
    }}
    * {{ box-sizing: border-box; }}
    body {{ min-width: 320px; min-height: 100dvh; margin: 0; background: var(--bg); color: var(--text); }}
    body > article {{ width: min(100% - 2rem, 880px); margin: 0 auto; padding: clamp(1.5rem, 4vw, 3.5rem) 0 4rem; }}
    .review-heading {{ display: flex; align-items: baseline; justify-content: space-between; border-bottom: 1px solid var(--line); padding-bottom: 1rem; }}
    .context, .progress {{ margin: 0; color: var(--muted); font-size: .82rem; font-weight: 700; letter-spacing: .08em; text-transform: uppercase; }}
    main {{ display: grid; gap: 2rem; padding-top: clamp(2rem, 6vw, 4.5rem); }}
    .scenario {{ max-width: 68ch; }}
    h1 {{ max-width: 22ch; margin: 0; font-size: clamp(2rem, 5vw, 3.8rem); letter-spacing: -.045em; line-height: 1; }}
    .scenario p, .complete p:last-child {{ color: var(--muted); font-size: 1.05rem; line-height: 1.65; }}
    form {{ display: grid; gap: 1.5rem; }}
    fieldset {{ min-width: 0; margin: 0; border: 0; padding: 0; }}
    legend {{ margin-bottom: .8rem; font-weight: 750; }}
    .choices {{ display: grid; gap: .65rem; }}
    .choice {{ display: grid; grid-template-columns: auto auto minmax(0, 1fr); align-items: start; gap: .8rem; border: 1px solid var(--line); border-radius: .75rem; background: var(--surface); padding: 1rem; line-height: 1.5; cursor: pointer; }}
    .choice:has(input:checked) {{ border-color: var(--accent); outline: 2px solid var(--accent); outline-offset: 2px; }}
    input {{ margin-top: .25rem; accent-color: var(--accent); }}
    .letter {{ color: var(--muted); font-weight: 800; }}
    .ambiguity {{ display: flex; flex-wrap: wrap; gap: .8rem 1.5rem; }}
    .ambiguity legend {{ width: 100%; }}
    .ambiguity label {{ display: inline-flex; align-items: center; gap: .45rem; }}
    button {{ justify-self: start; border: 1px solid var(--accent); border-radius: .55rem; background: var(--accent); color: var(--accent-text); padding: .85rem 1.1rem; font-weight: 800; cursor: pointer; }}
    button:active {{ transform: translateY(1px); }}
    button:focus-visible, input:focus-visible {{ outline: 3px solid var(--accent); outline-offset: 3px; }}
    .notice {{ margin: -.7rem 0 0; color: var(--muted); font-size: .85rem; }}
    .complete {{ max-width: 60ch; padding-top: clamp(3rem, 12vw, 9rem); }}
    .complete h1 {{ margin-top: .75rem; }}
    @media (max-width: 640px) {{
      body > article {{ width: min(100% - 1.25rem, 880px); padding-top: 1rem; }}
      main {{ gap: 1.5rem; padding-top: 2rem; }}
      .choice {{ grid-template-columns: auto minmax(0, 1fr); }}
      .choice input {{ grid-row: span 2; }}
    }}
    @media (prefers-reduced-motion: reduce) {{ * {{ scroll-behavior: auto !important; }} }}
  </style>
</head>
<body>
  <article>{content}</article>
</body>
</html>"""


def _load_object(path: pathlib.Path) -> dict:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"{path.name} must contain one JSON object")
    return payload


def _atomic_write(path: pathlib.Path, payload: dict) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.chmod(0o600)
    temporary.replace(path)


if __name__ == "__main__":
    raise SystemExit(main())
