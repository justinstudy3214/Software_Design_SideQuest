"""
Frontend delivery.
This is the "front end" half of the server: it hands the browser the
HTML/CSS/JS files from public/ and nothing else. It knows only whether a
user is logged in (to decide between index.html and login.html) - all
actual account/game logic lives in the backend modules (db, auth, api).
"""
import mimetypes
from urllib.parse import unquote

from config import PUBLIC_DIR


def serve_static(handler, path):
    """
    handler: the Handler instance (needs .current_user(), .redirect(),
    .send_json(), .security_headers(), .send_response/.send_header/.wfile
    from BaseHTTPRequestHandler).
    """
    user = handler.current_user()
    if path in ("/", "/index.html"):
        if not user:
            return handler.redirect("/login.html")
        rel = "index.html"
    elif path == "/login.html":
        if user:
            return handler.redirect("/")
        rel = "login.html"
    else:
        rel = unquote(path).lstrip("/")

    target = (PUBLIC_DIR / rel).resolve()
    if PUBLIC_DIR not in target.parents or not target.is_file():
        return handler.send_json(404, {"error": "Not found."})

    ctype = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
    if ctype.startswith("text/") or ctype in ("application/javascript", "application/json"):
        ctype += "; charset=utf-8"
    data = target.read_bytes()
    handler.send_response(200)
    handler.send_header("Content-Type", ctype)
    handler.send_header("Content-Length", str(len(data)))
    handler.send_header("Cache-Control", "no-store" if target.suffix == ".html" else "no-cache")
    handler.security_headers()
    handler.end_headers()
    handler.wfile.write(data)
