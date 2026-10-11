"""Bookie+ – Docker ebook manager with Material Design 3 UI."""
import io
import json
import math
import os
import re
import secrets
import logging
import stat
import time
import urllib.request
import urllib.error
import zipfile
import xml.etree.ElementTree as ET
from datetime import timedelta, date
from pathlib import Path

import mimetypes
import crypto

mimetypes.add_type("application/javascript", ".mjs")


DATA_DIR = Path(os.environ.get("DATA_DIR", "data"))
DEFAULT_DB_FILENAME = "bookie.db"
LEGACY_DB_FILENAME = "booker.db"


def _get_or_create_secret_key() -> str:
    key_file = DATA_DIR / "secret_key"
    key_file.parent.mkdir(parents=True, exist_ok=True)
    if key_file.exists():
        return key_file.read_text().strip()
    key = secrets.token_hex(32)
    key_file.write_text(key)
    key_file.chmod(stat.S_IRUSR | stat.S_IWUSR)
    return key


# ---------------------------------------------------------------------------
# Embedded metadata extraction
# ---------------------------------------------------------------------------

def extract_embedded_metadata(file_path: Path, ext: str) -> dict:
    """Extract title/author/etc. from embedded EPUB, PDF, or audio file metadata."""
    if ext == "epub":
        return _extract_epub_metadata(file_path)
    elif ext == "pdf":
        return _extract_pdf_metadata(file_path)
    elif ext in ("mp3", "m4b", "m4a", "aac", "flac", "ogg", "wma", "opus"):
        return _extract_audio_metadata(file_path)
    return {}


def _extract_epub_metadata(path: Path) -> dict:
    try:
        with zipfile.ZipFile(str(path)) as zf:
            # Locate OPF from container.xml
            with zf.open("META-INF/container.xml") as f:
                croot = ET.parse(f).getroot()
            ns_c = {"c": "urn:oasis:names:tc:opendocument:xmlns:container"}
            rf = croot.find(".//c:rootfile", ns_c)
            if rf is None:
                return {}
            opf_path = rf.get("full-path", "")
            with zf.open(opf_path) as f:
                oroot = ET.parse(f).getroot()

        ns = {
            "dc": "http://purl.org/dc/elements/1.1/",
            "opf": "http://www.idpf.org/2007/opf",
        }

        def dc(tag):
            el = oroot.find(f".//dc:{tag}", ns)
            return el.text.strip() if el is not None and el.text else None

        # ISBN from dc:identifier
        isbn10 = isbn13 = None
        for el in oroot.findall(".//dc:identifier", ns):
            scheme = (el.get("{http://www.idpf.org/2007/opf}scheme") or "").lower()
            val = re.sub(r"[-\s]", "", el.text or "")
            if val.isdigit():
                if len(val) == 13 and isbn13 is None:
                    isbn13 = val
                elif len(val) == 10 and isbn10 is None:
                    isbn10 = val
            elif "isbn" in scheme:
                if val.isdigit() and len(val) == 13:
                    isbn13 = val
                elif val.isdigit() and len(val) == 10:
                    isbn10 = val

        # Creators: primary author is first without refine role or role=aut
        authors = []
        for el in oroot.findall(".//dc:creator", ns):
            if el.text:
                authors.append(el.text.strip())
        author = authors[0] if authors else None

        date_raw = dc("date")
        pub_date = date_raw[:4] if date_raw else None

        return {
            "title": dc("title"),
            "author": author,
            "publisher": dc("publisher"),
            "language": dc("language"),
            "description": dc("description"),
            "published_date": pub_date,
            "isbn": isbn10,
            "isbn13": isbn13,
        }
    except Exception as exc:
        logger.debug("EPUB metadata extraction failed: %s", exc)
        return {}


def _extract_pdf_metadata(path: Path) -> dict:
    try:
        from pypdf import PdfReader
        reader = PdfReader(str(path))
        info = reader.metadata or {}

        def clean(val):
            return val.strip() if isinstance(val, str) and val.strip() else None

        raw_date = info.get("/CreationDate", "")
        pub_date = None
        if raw_date and len(raw_date) >= 6:
            # D:YYYYMMDDHHmmSS format
            digits = re.sub(r"[^0-9]", "", raw_date[:10])
            if len(digits) >= 4:
                pub_date = digits[:4]

        return {
            "title": clean(info.get("/Title")),
            "author": clean(info.get("/Author")),
            "publisher": clean(info.get("/Creator") or info.get("/Producer")),
            "description": clean(info.get("/Subject")),
            "published_date": pub_date,
        }
    except Exception as exc:
        logger.debug("PDF metadata extraction failed: %s", exc)
        return {}


def _extract_chapters_via_ffprobe(filepath: str) -> list[dict]:
    """Extract chapter markers via ffprobe if available in system PATH."""
    import subprocess
    import json
    try:
        res = subprocess.run(
            ["ffprobe", "-v", "quiet", "-print_format", "json", "-show_chapters", filepath],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if res.returncode == 0:
            data = json.loads(res.stdout)
            chapters = []
            for ch in data.get("chapters", []):
                start_time = float(ch.get("start_time", 0.0))
                tags = ch.get("tags", {})
                title = tags.get("title", f"Chapter {len(chapters)+1}")
                chapters.append({
                    "title": title,
                    "start_time": start_time
                })
            return chapters
    except Exception:
        pass
    return []


def _extract_audio_metadata(path: Path) -> dict:
    """Extract metadata from MP3, M4B, FLAC, and other audio formats."""
    try:
        from mutagen import File
        audio = File(str(path))
        if audio is None:
            return {}

        # Helper to safely get tag values, decoding mutagen freeform types
        def get_tag(key, fallback_keys=None):
            val = audio.get(key)
            if not val and fallback_keys:
                for fk in fallback_keys:
                    val = audio.get(fk)
                    if val:
                        break
            if not val:
                return None
            if isinstance(val, list):
                if not val:
                    return None
                val = val[0]
            # Convert mutagen specific types
            if val.__class__.__name__ == "MP4FreeForm":
                return bytes(val).decode("utf-8", errors="replace").strip()
            if isinstance(val, bytes):
                return val.decode("utf-8", errors="replace").strip()
            # ID3 text frame
            if hasattr(val, "text"):
                t = val.text
                if isinstance(t, list) and t:
                    return str(t[0]).strip()
                return str(t).strip()
            return str(val).strip()

        duration = None
        try:
            if audio.info and hasattr(audio.info, 'length'):
                duration = int(audio.info.length)
        except Exception:
            pass

        # Extract Chapters
        chapters = _extract_chapters_via_ffprobe(str(path))
        if not chapters:
            # Fallback 1: Nero-style MP4 chapters
            if hasattr(audio, "chapters") and audio.chapters:
                try:
                    for ch in audio.chapters:
                        start_time = float(getattr(ch, "start", 0))
                        # mutagen sometimes returns start as milliseconds
                        if start_time > 10000:
                            start_time /= 1000.0
                        title = getattr(ch, "title", f"Chapter {len(chapters)+1}")
                        chapters.append({
                            "title": title,
                            "start_time": start_time
                        })
                except Exception as e:
                    logger.debug("Failed to extract MP4 chapters: %s", e)
            # Fallback 2: ID3 CHAP chapters (MP3, etc.)
            elif hasattr(audio, "getall"):
                try:
                    chaps = audio.getall("CHAP")
                    if chaps:
                        for chap in chaps:
                            title = f"Chapter {len(chapters)+1}"
                            if hasattr(chap, "sub_frames") and chap.sub_frames:
                                tit2 = chap.sub_frames.get("TIT2")
                                if tit2 and hasattr(tit2, "text") and tit2.text:
                                    title = str(tit2.text[0])
                            start_time = float(chap.start_time) / 1000.0 if hasattr(chap, "start_time") else 0.0
                            chapters.append({
                                "title": title,
                                "start_time": start_time
                            })
                        chapters.sort(key=lambda x: x["start_time"])
                except Exception as e:
                    logger.debug("Failed to extract ID3 chapters: %s", e)

        return {
            "title": get_tag("TIT2", ["©nam", "Title"]),  # Title
            "author": get_tag("TPE1", ["©ART", "Artist"]),  # Performer/Artist
            "narrator": get_tag("TPE2", ["©NAR", "©nrt"]),  # Narrator/Band/Narrator Tag
            "description": get_tag("TIT3", ["©cmt", "©des", "Description"]),  # Subtitle/Comments/Description
            "publisher": get_tag("TPUB", ["©pub", "----:com.pilabor.tone:PUBLISHER", "Publisher"]),  # Publisher
            "published_date": get_tag("TDRC", ["©day", "Year"]),  # Recording date/Year
            "duration": duration,
            "series": get_tag("----:com.pilabor.tone:SERIES"),
            "series_order": get_tag("----:com.pilabor.tone:PART"),
            "language": get_tag("----:com.pilabor.tone:LANGUAGE", ["TLAN", "Language"]),
            "chapters": chapters,
        }
    except Exception as exc:
        logger.debug("Audio metadata extraction failed: %s", exc)
        return {}

from flask import (
    Flask,
    jsonify,
    request,
    send_file,
    send_from_directory,
    redirect,
    session,
    abort,
)
from werkzeug.utils import secure_filename

from sqlalchemy.orm import subqueryload
from models import db, Book, Settings, EmailAddress, Tag, BookTag
from auth import login_required, register_auth_routes
import scraper
import covers as cover_mgr
import mailer
import renamer

# Set the root logger to INFO so that INFO+ records are captured from the start.
# logging.basicConfig() is a no-op when any handler is already registered, so
# we configure the root logger explicitly.
_LOG_FORMAT = "%(asctime)s %(levelname)-8s %(name)s: %(message)s"
_root_logger = logging.getLogger()
_root_logger.setLevel(logging.INFO)

# Attach a StreamHandler so INFO+ records reach the terminal.  Only add one if
# the root logger has no StreamHandler yet — gunicorn/uWSGI add their own
# handler before importing the app, so we skip it to avoid duplicate output.
if not any(isinstance(h, logging.StreamHandler) for h in _root_logger.handlers):
    _console_handler = logging.StreamHandler()
    _console_handler.setFormatter(logging.Formatter(_LOG_FORMAT))
    _root_logger.addHandler(_console_handler)

# Silence chatty library loggers so DEBUG/INFO mode doesn't flood the output
# with irrelevant framework noise.
for _noisy_logger in ("werkzeug", "sqlalchemy.engine", "sqlalchemy.pool",
                      "urllib3", "PIL", "asyncio"):
    logging.getLogger(_noisy_logger).setLevel(logging.WARNING)

logger = logging.getLogger(__name__)

# In-memory log buffer — keeps the last N lines so the Logs tab has something
# to show.  Size is intentionally modest: even at DEBUG the suppressed library
# loggers mean we only store app-level records.
_LOG_BUFFER_CAPACITY = 500

class _LogBuffer(logging.Handler):
    def __init__(self, capacity: int = _LOG_BUFFER_CAPACITY):
        super().__init__()
        self._buf: list[str] = []
        self._cap = capacity
    def emit(self, record: logging.LogRecord) -> None:
        self._buf.append(self.format(record))
        if len(self._buf) > self._cap:
            self._buf = self._buf[-self._cap:]
    def get_lines(self) -> list[str]:
        return list(self._buf)

_log_buffer = _LogBuffer()
_log_buffer.setFormatter(logging.Formatter(_LOG_FORMAT))
logging.getLogger().addHandler(_log_buffer)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

BOOKS_DIR = DATA_DIR / "books"
AUDIOBOOKS_DIR = DATA_DIR / "audiobooks"
COVERS_DIR = DATA_DIR / "covers"
ALLOWED_EXTENSIONS = {"epub", "pdf", "mobi", "azw", "azw3", "fb2", "djvu", "cbz", "cbr", "txt",
                      "mp3", "m4b", "m4a", "aac", "flac", "ogg", "wma", "opus"}
AUDIOBOOK_EXTENSIONS = {"mp3", "m4b", "m4a", "aac", "flac", "ogg", "wma", "opus"}
MAX_UPLOAD_MB = 128
MAX_AUDIOBOOK_MB = 500  # Larger limit for audiobooks


def get_book_dir(book) -> Path:
    """Return the base directory (target or fallback) for a book."""
    target_dir = AUDIOBOOKS_DIR if book.is_audiobook else BOOKS_DIR
    if (target_dir / book.filename).exists():
        return target_dir
    fallback_dir = BOOKS_DIR if book.is_audiobook else AUDIOBOOKS_DIR
    if (fallback_dir / book.filename).exists():
        return fallback_dir
    return target_dir


def get_book_path(book) -> Path:
    """Return the absolute path of a book, handling fallback for old paths."""
    target_dir = AUDIOBOOKS_DIR if book.is_audiobook else BOOKS_DIR
    target_path = target_dir / book.filename
    if target_path.exists():
        return target_path
    fallback_dir = BOOKS_DIR if book.is_audiobook else AUDIOBOOKS_DIR
    fallback_path = fallback_dir / book.filename
    if fallback_path.exists():
        return fallback_path
    return target_path


def _ensure_correct_base_dir(book, current_path: Path) -> tuple[Path, Path]:
    """Ensure the book file is in its correct target base directory. Returns (new_current_path, base_dir)."""
    target_dir = AUDIOBOOKS_DIR if book.is_audiobook else BOOKS_DIR
    if not current_path.exists():
        return current_path, target_dir
    try:
        current_path.resolve().relative_to(target_dir.resolve())
        return current_path, target_dir
    except ValueError:
        # Move it to the target directory structure
        dest = target_dir / current_path.name
        counter = 1
        while dest.exists():
            dest = target_dir / f"{current_path.stem}_{counter}{current_path.suffix}"
            counter += 1
        dest.parent.mkdir(parents=True, exist_ok=True)
        current_path.rename(dest)
        book.filename = dest.name
        _cleanup_empty_dirs(current_path.parent)
        from models import db as _db
        _db.session.commit()
        return dest, target_dir


# Magic-byte signatures for formats where we can reliably verify content.
# Extensions not listed here are accepted on extension alone (txt, fb2, mobi, etc.
# have no universal single-byte signature that is safe to enforce).
_MAGIC_BYTES: dict[str, list[bytes]] = {
    "epub": [b"PK\x03\x04"],           # EPUB is a ZIP archive
    "pdf":  [b"%PDF"],
    "cbz":  [b"PK\x03\x04"],           # CBZ is a ZIP archive
    "cbr":  [b"Rar!\x1a\x07\x00",      # RAR v4
             b"Rar!\x1a\x07\x01\x00",  # RAR v5
             b"PK\x03\x04"],            # some CBRs are actually ZIPs
    "djvu": [b"AT&TFORM"],
    "mp3":  [b"\xff\xfb", b"\xff\xfa", b"ID3"],  # MP3 syncword or ID3 tag
    "m4b":  [b"\x00\x00\x00\x18ftypM4B"],        # M4B atom signature
    "m4a":  [b"\x00\x00\x00\x20ftypisom"],       # M4A ISO Base Media signature
    "flac": [b"fLaC"],                            # FLAC signature
    "ogg":  [b"OggS"],                            # OGG signature
}
_MAGIC_READ_BYTES = 8  # max prefix length we need to read


def _magic_ok(file_storage, ext: str) -> bool:
    """Return True if the uploaded file's header matches the expected format."""
    sigs = _MAGIC_BYTES.get(ext)
    if not sigs:
        return True  # no check defined for this extension
    header = file_storage.read(_MAGIC_READ_BYTES)
    file_storage.seek(0)
    return any(header.startswith(sig) for sig in sigs)


_EMAIL_RE = re.compile(r'^[^@\s]+@[^@\s]+\.[^@\s]+$')

def _valid_email(value: str) -> bool:
    return bool(_EMAIL_RE.match(value))


def _safe_int(value, default: int) -> int:
    """Convert *value* to int, returning *default* on failure."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _cleanup_empty_dirs(directory: Path) -> None:
    """Remove *directory* and its parent if both are empty and not BOOKS_DIR/AUDIOBOOKS_DIR."""
    try:
        for folder in [directory, directory.parent]:
            if folder not in (BOOKS_DIR, AUDIOBOOKS_DIR) and folder.exists() and not any(folder.iterdir()):
                folder.rmdir()
    except Exception as exc:
        logger.debug("Could not remove empty directory %s: %s", directory, exc)



# ---------------------------------------------------------------------------
# Update check
# ---------------------------------------------------------------------------

_UPDATE_CHECK_TTL = 3600  # seconds
_update_check_cache: tuple[float, dict] | None = None


def _check_for_update() -> dict:
    """Query GHCR for the latest image creation date and compare to BUILD_DATE."""
    global _update_check_cache

    now = time.time()
    if _update_check_cache and (now - _update_check_cache[0]) < _UPDATE_CHECK_TTL:
        return _update_check_cache[1]

    build_date = os.environ.get("BUILD_DATE", "").strip()
    if not build_date:
        result: dict = {"update_available": False, "reason": "no_build_date"}
        _update_check_cache = (now, result)
        return result

    ghcr_image = os.environ.get("GHCR_IMAGE", "ghcr.io/OmegaRa/bookie-plus").strip()
    # Expect format: registry/owner/image  (e.g. ghcr.io/OmegaRa/bookie-plus)
    slash_idx = ghcr_image.find("/")
    if slash_idx == -1:
        result = {"update_available": False, "reason": "invalid_image"}
        _update_check_cache = (now, result)
        return result

    registry = ghcr_image[:slash_idx]
    image_path = ghcr_image[slash_idx + 1:]

    try:
        # 1. Obtain an anonymous pull token from the registry
        token_url = f"https://{registry}/token?scope=repository:{image_path}:pull"
        with urllib.request.urlopen(token_url, timeout=10) as resp:
            token_data = json.loads(resp.read())
        token = token_data.get("token") or token_data.get("access_token", "")

        # 2. Fetch the manifest for the 'latest' tag
        manifest_url = f"https://{registry}/v2/{image_path}/manifests/latest"
        manifest_req = urllib.request.Request(
            manifest_url,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": (
                    "application/vnd.oci.image.manifest.v1+json,"
                    "application/vnd.docker.distribution.manifest.v2+json"
                ),
            },
        )
        with urllib.request.urlopen(manifest_req, timeout=10) as resp:
            manifest = json.loads(resp.read())

        config_digest = manifest.get("config", {}).get("digest", "")
        if not config_digest:
            result = {"update_available": False, "reason": "no_config_digest"}
            _update_check_cache = (now, result)
            return result

        # 3. Fetch the config blob to read the image creation timestamp
        blob_url = f"https://{registry}/v2/{image_path}/blobs/{config_digest}"
        blob_req = urllib.request.Request(
            blob_url,
            headers={"Authorization": f"Bearer {token}"},
        )
        with urllib.request.urlopen(blob_req, timeout=10) as resp:
            config = json.loads(resp.read())

        # Prefer the OCI label (set at pipeline start, same moment as BUILD_DATE)
        # over config["created"] which is set when Docker build finishes (always later).
        labels = config.get("config", {}).get("Labels") or {}
        latest_created = labels.get("org.opencontainers.image.created", "") or config.get("created", "")
        # ISO-8601 strings compare lexicographically
        update_available = bool(latest_created and latest_created > build_date)
        result = {
            "update_available": update_available,
            "current_build": build_date,
            "latest_build": latest_created,
        }
    except Exception as exc:
        logger.debug("Update check failed: %s", exc)
        result = {"update_available": False, "reason": "check_failed"}

    _update_check_cache = (now, result)
    return result


def create_app():
    _db_env = os.environ.get("DATABASE_URL")
    if not _db_env:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        _legacy_db = DATA_DIR / LEGACY_DB_FILENAME
        _default_db = DATA_DIR / DEFAULT_DB_FILENAME
        if _legacy_db.exists() and not _default_db.exists():
            _legacy_db.replace(_default_db)

    app = Flask(__name__)
    app.config["SQLALCHEMY_DATABASE_URI"] = os.environ.get(
        "DATABASE_URL", f"sqlite:///{(DATA_DIR / DEFAULT_DB_FILENAME).absolute()}"
    )
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
    app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_MB * 1024 * 1024
    app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY") or _get_or_create_secret_key()
    app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(days=30)
    app.config["SESSION_COOKIE_HTTPONLY"] = True
    app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
    _secure_env = os.environ.get("SESSION_COOKIE_SECURE", "").lower()
    if _secure_env in ("1", "true", "yes"):
        app.config["SESSION_COOKIE_SECURE"] = True
    else:
        app.config["SESSION_COOKIE_SECURE"] = False

    db.init_app(app)

    with app.app_context():
        BOOKS_DIR.mkdir(parents=True, exist_ok=True)
        AUDIOBOOKS_DIR.mkdir(parents=True, exist_ok=True)
        COVERS_DIR.mkdir(parents=True, exist_ok=True)
        db.create_all()
        _migrate_db(app)
        _fill_missing_audiobook_metadata(app)
        # Restore persisted log level (defaults to INFO on first run)
        _saved_level = (Settings.get("log_level") or "INFO").upper()
        _saved_numeric = getattr(logging, _saved_level, logging.INFO)
        logging.getLogger().setLevel(_saved_numeric)

    # Security headers
    @app.after_request
    def set_security_headers(response):
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
        response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        return response

    # Register auth routes
    register_auth_routes(app, Settings)

    # -----------------------------------------------------------------------
    # Frontend – serve React SPA
    # -----------------------------------------------------------------------

    _REACT_DIST = Path(__file__).parent / "static" / "dist"

    @app.route("/static/site.webmanifest")
    def web_manifest():
        resp = send_from_directory("static", "site.webmanifest")
        resp.headers["Content-Type"] = "application/manifest+json"
        resp.headers["Cache-Control"] = "no-cache"
        return resp

    @app.route("/sw.js")
    def service_worker():
        """Serve SW from root scope so it can control all pages."""
        resp = send_from_directory("static", "sw.js")
        resp.headers["Service-Worker-Allowed"] = "/"
        resp.headers["Cache-Control"] = "no-cache"
        return resp

    @app.route("/", defaults={"path": ""})
    @app.route("/<path:path>")
    def serve_frontend(path):
        # Never intercept API routes
        if path.startswith("api/") or path.startswith("static/"):
            abort(404)
        target = _REACT_DIST / path
        if path and target.exists() and target.is_file():
            return send_from_directory(_REACT_DIST, path)
        return send_from_directory(_REACT_DIST, "index.html")

    # -----------------------------------------------------------------------
    # Books – CRUD
    # -----------------------------------------------------------------------

    @app.route("/api/books", methods=["GET"])
    @login_required
    def list_books():
        query = Book.query
        search = request.args.get("q", "").strip()
        if search:
            # Escape LIKE metacharacters so they're treated as literals
            escaped = search.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            like = f"%{escaped}%"
            query = query.filter(
                db.or_(
                    Book.title.ilike(like),
                    Book.author.ilike(like),
                    Book.series.ilike(like),
                    Book.isbn.ilike(like),
                    Book.isbn13.ilike(like),
                )
            )
        fmt = request.args.get("format")
        if fmt:
            query = query.filter(Book.file_format == fmt.lower())
        series_filter = request.args.get("series")
        if series_filter:
            query = query.filter(Book.series == series_filter)
        lang = request.args.get("language")
        if lang:
            query = query.filter(Book.language == lang)
        tag = request.args.get("tag")
        if tag:
            query = query.join(BookTag, BookTag.book_id == Book.id).join(Tag, Tag.id == BookTag.tag_id).filter(Tag.name == tag)
        _SORT_COLS = {"author", "title", "type", "series", "published_date", "date_added", "file_size", "rating"}
        sort = request.args.get("sort", "author")
        if sort not in _SORT_COLS:
            sort = "author"
        order = request.args.get("order", "asc")
        if sort == "series":
            # Sort nulls last, then by series name, then by series order
            if order == "desc":
                query = query.order_by(
                    db.case((Book.series.is_(None), 1), else_=0).asc(),
                    Book.series.desc(),
                    db.case((Book.series_order.is_(None), 1), else_=0).asc(),
                    Book.series_order.desc(),
                )
            else:
                query = query.order_by(
                    db.case((Book.series.is_(None), 1), else_=0).asc(),
                    Book.series.asc(),
                    db.case((Book.series_order.is_(None), 1), else_=0).asc(),
                    Book.series_order.asc(),
                )
        elif sort == "author":
            # Author primary, then series name + order as secondary (nulls last), then title
            if order == "desc":
                query = query.order_by(
                    Book.author.desc(),
                    db.case((Book.series.is_(None), 1), else_=0).asc(),
                    Book.series.asc(),
                    db.case((Book.series_order.is_(None), 1), else_=0).asc(),
                    Book.series_order.asc(),
                    Book.title.desc(),
                )
            else:
                query = query.order_by(
                    Book.author.asc(),
                    db.case((Book.series.is_(None), 1), else_=0).asc(),
                    Book.series.asc(),
                    db.case((Book.series_order.is_(None), 1), else_=0).asc(),
                    Book.series_order.asc(),
                    Book.title.asc(),
                )
        elif sort == "type":
            # Sort by is_audiobook (ebooks vs audiobooks), then author, then title
            if order == "desc":
                query = query.order_by(
                    Book.is_audiobook.desc(),
                    Book.author.desc(),
                    Book.title.desc()
                )
            else:
                query = query.order_by(
                    Book.is_audiobook.asc(),
                    Book.author.asc(),
                    Book.title.asc()
                )
        elif sort == "title":
            # Sort by title, stripping leading articles (A, An, The)
            sort_key = db.case(
                (Book.title.ilike("the %"), db.func.substr(Book.title, 5)),
                (Book.title.ilike("an %"), db.func.substr(Book.title, 4)),
                (Book.title.ilike("a %"), db.func.substr(Book.title, 3)),
                else_=Book.title,
            )
            query = query.order_by(sort_key.desc() if order == "desc" else sort_key.asc())
        else:
            col = getattr(Book, sort, None)
            if col is not None:
                query = query.order_by(col.desc() if order == "desc" else col.asc())
        page = max(1, request.args.get("page", 1, type=int))
        per_page = min(max(1, request.args.get("per_page", 40, type=int)), 200)
        paginated = query.paginate(page=page, per_page=per_page, error_out=False)
        return jsonify({
            "books": [b.to_dict() for b in paginated.items],
            "total": paginated.total,
            "pages": paginated.pages,
            "page": page,
        })

    @app.route("/api/books/<int:book_id>", methods=["GET"])
    @login_required
    def get_book(book_id):
        book = Book.query.get_or_404(book_id)
        return jsonify(book.to_dict())

    @app.route("/api/books/scan", methods=["POST"])
    @login_required
    def scan_books():
        """Walk BOOKS_DIR (for ebooks) and AUDIOBOOKS_DIR (for audiobooks), register new files, and remove DB entries for deleted files."""
        # ── Remove stale records (file deleted from disk) ──────────────────
        removed = 0
        for book in Book.query.all():
            path = get_book_path(book)
            if not path.exists():
                cover_mgr.delete_cover(book.id)
                db.session.delete(book)
                removed += 1
        db.session.commit()

        # ── Register new files ─────────────────────────────────────────────
        known = {b.filename for b in Book.query.with_entities(Book.filename).all()}
        auto_meta = Settings.get("auto_metadata", "false") == "true"
        added = 0

        # Scan targets: (directory, is_audio_only)
        scan_targets = [
            (BOOKS_DIR, False),
            (AUDIOBOOKS_DIR, True)
        ]

        for scan_dir, is_audio_only in scan_targets:
            if not scan_dir.exists():
                continue
            for path in scan_dir.rglob("*"):
                if not path.is_file():
                    continue
                ext = path.suffix.lstrip(".").lower()
                if ext not in ALLOWED_EXTENSIONS:
                    continue
                
                is_audio = ext in AUDIOBOOK_EXTENSIONS
                if is_audio_only != is_audio:
                    continue

                rel = str(path.relative_to(scan_dir))
                if rel in known:
                    continue

                try:
                    book = Book(
                        filename=rel,
                        file_format=ext,
                        file_size=path.stat().st_size,
                        title=path.stem,
                        is_audiobook=is_audio,
                        audio_format=ext if is_audio else None,
                    )
                    db.session.add(book)
                    db.session.flush()

                    # Extract embedded cover
                    cover_data = None
                    if ext == "epub":
                        cover_data = cover_mgr.extract_cover_from_epub(str(path))
                    elif ext == "pdf":
                        cover_data = cover_mgr.extract_cover_from_pdf(str(path))
                    elif is_audio:
                        cover_data = cover_mgr.extract_cover_from_audio(str(path))
                    if cover_data:
                        cf = cover_mgr.save_cover(book.id, cover_data)
                        if cf:
                            book.cover_filename = cf

                    # Extract and apply embedded metadata (title, author, ISBN, …)
                    embedded = extract_embedded_metadata(path, ext)
                    if any(embedded.values()):
                        _apply_metadata(book, embedded, replace_missing_only=False)

                    # Auto-fetch from online sources if the setting is on
                    if auto_meta:
                        try:
                            _auto_fetch_metadata(book)
                        except Exception as exc:
                            logger.warning("Auto-fetch failed for scanned book %s: %s", rel, exc)

                    # Apply rename/organize scheme
                    try:
                        _rename_and_organize(book, path)
                    except Exception as exc:
                        logger.warning("Rename failed for scanned book %s: %s", rel, exc)

                    added += 1
                    known.add(book.filename)  # use updated filename after rename
                except Exception as e:
                    logger.exception("Error registering scanned book %s", path)

        db.session.commit()
        return jsonify({"added": added, "removed": removed})


    @app.route("/api/books/upload", methods=["POST"])
    @login_required
    def upload_book():
        if "file" not in request.files:
            return jsonify({"error": "No file provided"}), 400
        file = request.files["file"]
        if not file.filename:
            return jsonify({"error": "Empty filename"}), 400

        ext = file.filename.rsplit(".", 1)[-1].lower() if "." in file.filename else ""
        if ext not in ALLOWED_EXTENSIONS:
            return jsonify({"error": f"Unsupported format: {ext}"}), 400
        if not _magic_ok(file, ext):
            return jsonify({"error": f"File content does not match declared format (.{ext})"}), 400

        filename = secure_filename(file.filename)
        is_audio = ext in AUDIOBOOK_EXTENSIONS
        dest_dir = AUDIOBOOKS_DIR if is_audio else BOOKS_DIR
        dest = dest_dir / filename
        counter = 1
        stem = Path(filename).stem
        while dest.exists():
            filename = f"{stem}_{counter}.{ext}"
            dest = dest_dir / filename
            counter += 1

        file.save(str(dest))
        size = dest.stat().st_size

        book = Book(
            filename=filename,
            file_format=ext,
            file_size=size,
            title=Path(filename).stem,
            is_audiobook=is_audio,
            audio_format=ext if is_audio else None,
        )
        db.session.add(book)
        db.session.commit()

        # Extract embedded cover
        cover_data = None
        if ext == "epub":
            cover_data = cover_mgr.extract_cover_from_epub(str(dest))
        elif ext == "pdf":
            cover_data = cover_mgr.extract_cover_from_pdf(str(dest))
        elif is_audio:
            cover_data = cover_mgr.extract_cover_from_audio(str(dest))
        if cover_data:
            cf = cover_mgr.save_cover(book.id, cover_data)
            if cf:
                book.cover_filename = cf
                db.session.commit()

        # Apply embedded metadata first (title, author, ISBN, etc.)
        embedded = extract_embedded_metadata(dest, ext)
        if any(embedded.values()):
            _apply_metadata(book, embedded, replace_missing_only=False)

        # Auto-fetch from online sources to fill remaining gaps
        auto_meta = Settings.get("auto_metadata", "false")
        if auto_meta == "true":
            try:
                _auto_fetch_metadata(book)
            except Exception as exc:
                logger.warning("Auto-fetch metadata failed for uploaded book %s: %s", book.id, exc)

        # Apply renaming scheme + folder organization
        _rename_and_organize(book, dest)

        return jsonify(book.to_dict()), 201

    @app.route("/api/books/<int:book_id>", methods=["PUT"])
    @login_required
    def update_book(book_id):
        book = Book.query.get_or_404(book_id)
        data = request.get_json(silent=True)
        if data is None:
            data = {}
        elif not isinstance(data, dict):
            return jsonify({"error": "Invalid JSON payload"}), 400
        fields = [
            "title", "author", "published_date", "page_count",
            "series", "series_order", "rating", "narrator",
            "publisher", "language", "isbn", "isbn13", "description"
        ]
        for f in fields:
            if f not in data:
                continue
            val = data[f]
            if f == "published_date" and isinstance(val, str):
                val = val[:4] or None
            # Guard float fields against NaN / Infinity which would corrupt sorting
            if f in ("series_order", "rating") and val is not None:
                try:
                    val = float(val)
                except (TypeError, ValueError):
                    return jsonify({"error": f"Invalid value for {f}"}), 400
                if not math.isfinite(val):
                    return jsonify({"error": f"{f} must be a finite number"}), 400
            setattr(book, f, val)
        db.session.commit()
        # Re-apply rename/organize after metadata update
        file_path = get_book_path(book)
        if file_path.exists():
            try:
                _rename_and_organize(book, file_path)
            except Exception as exc:
                logger.warning("Rename after save failed for book %s: %s", book_id, exc)
        return jsonify(book.to_dict())

    @app.route("/api/books/<int:book_id>", methods=["DELETE"])
    @login_required
    def delete_book(book_id):
        book = Book.query.get_or_404(book_id)
        filepath = get_book_path(book)
        parent = filepath.parent
        if filepath.exists():
            filepath.unlink()
        _cleanup_empty_dirs(parent)
        cover_mgr.delete_cover(book_id)
        db.session.delete(book)
        db.session.commit()
        return jsonify({"success": True})

    @app.route("/api/books/ids", methods=["GET"])
    @login_required
    def get_book_ids():
        """Return all book IDs matching the current filters (no pagination)."""
        query = Book.query
        search = request.args.get("q", "").strip()
        if search:
            escaped = search.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            like = f"%{escaped}%"
            query = query.filter(db.or_(
                Book.title.ilike(like), Book.author.ilike(like),
                Book.series.ilike(like), Book.isbn.ilike(like), Book.isbn13.ilike(like),
            ))
        fmt = request.args.get("format")
        if fmt:
            query = query.filter(Book.file_format == fmt.lower())
        if series := request.args.get("series"):
            query = query.filter(Book.series == series)
        if tag := request.args.get("tag"):
            query = query.join(BookTag, BookTag.book_id == Book.id).join(Tag, Tag.id == BookTag.tag_id).filter(Tag.name == tag)
        return jsonify({"ids": [b.id for b in query.with_entities(Book.id).all()]})

    @app.route("/api/books/bulk-fetch-metadata", methods=["POST"])
    @login_required
    def bulk_fetch_metadata():
        """Fetch metadata for a list of book IDs, one at a time."""
        import time
        ids = (request.get_json(silent=True) or {}).get("ids", [])
        updated = 0
        for book_id in ids:
            book = Book.query.get(book_id)
            if not book:
                continue
            try:
                _auto_fetch_metadata(book)
            except Exception as exc:
                logger.warning("Bulk auto-fetch metadata failed for book %s: %s", book_id, exc)
            updated += 1
            if updated < len(ids):
                time.sleep(1)  # be polite to metadata sources
        return jsonify({"updated": updated})

    @app.route("/api/books/bulk-delete", methods=["POST"])
    @login_required
    def bulk_delete_books():
        ids = (request.get_json(silent=True) or {}).get("ids", [])
        deleted = 0
        for book_id in ids:
            book = Book.query.get(book_id)
            if not book:
                continue
            filepath = get_book_path(book)
            parent = filepath.parent
            if filepath.exists():
                filepath.unlink()
            _cleanup_empty_dirs(parent)
            cover_mgr.delete_cover(book_id)
            db.session.delete(book)
            deleted += 1
        db.session.commit()
        return jsonify({"deleted": deleted})

    @app.route("/api/books/bulk-tag", methods=["POST"])
    @login_required
    def bulk_add_tag():
        data = request.get_json(silent=True) or {}
        ids = data.get("ids", [])
        tag_name = (data.get("tag") or "").strip()
        if not tag_name:
            return jsonify({"error": "tag required"}), 400
        tag = Tag.query.filter_by(name=tag_name).first()
        if not tag:
            tag = Tag(name=tag_name)
            db.session.add(tag)
            db.session.flush()
        added = 0
        for book_id in ids:
            exists = BookTag.query.filter_by(book_id=book_id, tag_id=tag.id).first()
            if not exists:
                db.session.add(BookTag(book_id=book_id, tag_id=tag.id))
                added += 1
        db.session.commit()
        return jsonify({"added": added, "tag": tag.to_dict()})

    @app.route("/api/books/bulk-untag", methods=["POST"])
    @login_required
    def bulk_remove_tag():
        data = request.get_json(silent=True) or {}
        ids = data.get("ids", [])
        tag_name = (data.get("tag") or "").strip()
        if not tag_name:
            return jsonify({"error": "tag required"}), 400
        tag = Tag.query.filter_by(name=tag_name).first()
        if not tag:
            return jsonify({"removed": 0})
        removed = (
            BookTag.query
            .filter(BookTag.tag_id == tag.id, BookTag.book_id.in_(ids))
            .delete(synchronize_session=False)
        )
        db.session.commit()
        return jsonify({"removed": removed})

    @app.route("/api/books/bulk-tags-summary", methods=["POST"])
    @login_required
    def bulk_tags_summary():
        data = request.get_json(silent=True) or {}
        ids = data.get("ids", [])
        if not ids:
            return jsonify({"total_books": 0, "tags": []})
        results = (
            db.session.query(Tag.id, Tag.name, db.func.count(BookTag.book_id))
            .join(BookTag, Tag.id == BookTag.tag_id)
            .filter(BookTag.book_id.in_(ids))
            .group_by(Tag.id, Tag.name)
            .order_by(Tag.name.asc())
            .all()
        )
        return jsonify({
            "total_books": len(ids),
            "tags": [{"id": r[0], "name": r[1], "count": r[2]} for r in results],
        })

    @app.route("/api/books/bulk-update-tags", methods=["POST"])
    @login_required
    def bulk_update_tags():
        data = request.get_json(silent=True) or {}
        ids = data.get("ids", [])
        if not ids:
            return jsonify({"error": "ids required"}), 400

        add_tags = [t.strip() for t in data.get("add", []) if isinstance(t, str) and t.strip()]
        remove_tags = [t.strip() for t in data.get("remove", []) if isinstance(t, str) and t.strip()]
        replace = data.get("replace") or {}

        if replace and replace.get("old_tag") and replace.get("new_tag"):
            old_name = replace["old_tag"].strip()
            new_name = replace["new_tag"].strip()
            if old_name and new_name and old_name != new_name:
                remove_tags.append(old_name)
                add_tags.append(new_name)

        if remove_tags:
            tags_to_remove = Tag.query.filter(Tag.name.in_(remove_tags)).all()
            if tags_to_remove:
                tag_ids = [t.id for t in tags_to_remove]
                BookTag.query.filter(
                    BookTag.book_id.in_(ids),
                    BookTag.tag_id.in_(tag_ids),
                ).delete(synchronize_session=False)

        if add_tags:
            for name in set(add_tags):
                tag = Tag.query.filter_by(name=name).first()
                if not tag:
                    tag = Tag(name=name)
                    db.session.add(tag)
                    db.session.flush()

                existing = {
                    r[0] for r in db.session.query(BookTag.book_id)
                    .filter(BookTag.book_id.in_(ids), BookTag.tag_id == tag.id)
                    .all()
                }
                for book_id in ids:
                    if book_id not in existing:
                        db.session.add(BookTag(book_id=book_id, tag_id=tag.id))

        db.session.commit()
        return jsonify({"success": True, "updated": len(ids)})

    @app.route("/api/books/<int:book_id>/download", methods=["GET"])
    @login_required
    def download_book(book_id):
        book = Book.query.get_or_404(book_id)
        base_dir = get_book_dir(book)
        filepath = get_book_path(book).resolve()
        if not filepath.is_relative_to(base_dir.resolve()):
            abort(400)
        if not filepath.exists():
            abort(404)
        return send_file(str(filepath), as_attachment=True, download_name=Path(book.filename).name)

    @app.route("/api/audiobooks/<int:book_id>/stream", methods=["GET"])
    @login_required
    def stream_audiobook(book_id):
        """Stream audiobook with range request support for seeking."""
        book = Book.query.get_or_404(book_id)
        if not book.is_audiobook:
            abort(404)
        base_dir = get_book_dir(book)
        filepath = get_book_path(book).resolve()
        if not filepath.is_relative_to(base_dir.resolve()):
            abort(400)
        if not filepath.exists():
            abort(404)
        # Flask/Werkzeug automatically handles range requests (Range header)
        mimetype = f"audio/{book.audio_format}" if book.audio_format else "audio/mpeg"
        return send_file(str(filepath), mimetype=mimetype)

    @app.route("/api/audiobooks/<int:book_id>/metadata", methods=["GET"])
    @login_required
    def get_audiobook_metadata(book_id):
        """Get full audiobook metadata including chapters."""
        book = Book.query.get_or_404(book_id)
        if not book.is_audiobook:
            abort(404)
        return jsonify({
            "id": book.id,
            "title": book.title,
            "author": book.author,
            "narrator": book.narrator,
            "duration": book.duration,
            "format": book.audio_format,
            "chapters": json.loads(book.chapters) if book.chapters else [],
            "file_size": book.file_size,
        })

    @app.route("/api/books/<int:book_id>/progress", methods=["POST"])
    @login_required
    def save_book_progress(book_id):
        """Save read/listen progress and status for a book."""
        book = Book.query.get_or_404(book_id)
        data = request.get_json(silent=True) or {}
        
        progress = data.get("progress")
        progress_location = data.get("progress_location")
        read_status = data.get("read_status")
        
        if progress is not None:
            try:
                progress = max(0.0, min(1.0, float(progress)))
                book.progress = progress
            except (TypeError, ValueError):
                return jsonify({"error": "Invalid progress value"}), 400
                
        if progress_location is not None:
            book.progress_location = str(progress_location)
            
        if read_status is not None:
            if read_status not in ("unread", "reading", "finished"):
                return jsonify({"error": "Invalid read_status value"}), 400
            book.read_status = read_status
            
        db.session.commit()
        return jsonify(book.to_dict())

    @app.route("/api/books/<int:book_id>/rename", methods=["POST"])
    @login_required
    def rename_book(book_id):
        """Rename a specific book's file using a given scheme or custom template."""
        book = Book.query.get_or_404(book_id)
        data = request.get_json(silent=True) or {}
        scheme = data.get("scheme", "author_title")
        custom_tpl = data.get("custom_template", "")
        meta = {
            "title": book.title,
            "author": book.author,
            "published_date": book.published_date,
            "publisher": book.publisher,
            "isbn": book.isbn,
            "isbn13": book.isbn13,
            "language": book.language,
        }
        src = get_book_path(book)
        if not src.exists():
            return jsonify({"error": "File not found on disk"}), 404
        base_dir = get_book_dir(book)
        new_path, new_name = renamer.rename_book_file(src, base_dir, scheme, meta, custom_tpl)
        book.filename = new_name
        db.session.commit()
        return jsonify({"success": True, "new_filename": new_name})

    @app.route("/api/rename/preview", methods=["POST"])
    @login_required
    def rename_preview():
        """Preview how a naming scheme would rename a list of books."""
        data = request.get_json(silent=True) or {}
        scheme = data.get("scheme", "author_title")
        custom_tpl = data.get("custom_template", "")
        book_ids = data.get("book_ids", [])
        template = renamer.get_scheme_template(scheme, custom_tpl)
        previews = []
        for bid in book_ids:
            book = Book.query.get(bid)
            if not book:
                continue
            meta = {
                "title": book.title, "author": book.author,
                "published_date": book.published_date, "publisher": book.publisher,
                "isbn": book.isbn, "isbn13": book.isbn13, "language": book.language,
            }
            previews.append({
                "id": book.id,
                "original": book.filename,
                "preview": renamer.apply_scheme(template, book.filename, meta),
            })
        return jsonify(previews)

    @app.route("/api/rename/schemes", methods=["GET"])
    @login_required
    def list_schemes():
        return jsonify({
            "schemes": [
                {"key": k, "label": v, "template": renamer.SCHEMES.get(k)}
                for k, v in renamer.SCHEME_LABELS.items()
            ],
            "placeholders": [{"placeholder": p, "description": d} for p, d in renamer.PLACEHOLDERS],
        })

    @app.route("/api/rename/bulk", methods=["POST"])
    @login_required
    def bulk_rename():
        """Rename all books using the current naming scheme and folder structure. apply=true to commit."""
        data = request.get_json(silent=True) or {}
        apply = data.get("apply", False)
        scheme = Settings.get("rename_scheme", "original")
        custom_tpl = Settings.get("rename_custom_template", "")
        folder_mode = Settings.get("folder_organization", "flat")
        books = Book.query.all()
        results = []
        errors = []
        for book in books:
            src = get_book_path(book)
            if not src.exists():
                errors.append({"id": book.id, "original": book.filename, "error": "File not found"})
                continue
            meta = {
                "title": book.title, "author": book.author,
                "published_date": book.published_date, "publisher": book.publisher,
                "isbn": book.isbn, "isbn13": book.isbn13, "language": book.language,
                "series": getattr(book, "series", None),
                "series_index": getattr(book, "series_order", None),
            }
            template = renamer.get_scheme_template(scheme, custom_tpl)
            # Compute expected final name (after rename + folder)
            new_filename = renamer.apply_scheme(template, Path(book.filename).name, meta)
            if folder_mode != "flat" and book.author:
                author_safe = renamer._safe(book.author)
                series = getattr(book, "series", None)
                if folder_mode == "by_author_series" and series:
                    new_filename_with_folder = f"{author_safe}/{renamer._safe(series)}/{new_filename}"
                else:
                    new_filename_with_folder = f"{author_safe}/{new_filename}"
            else:
                new_filename_with_folder = new_filename

            # Check if currently in the correct base directory
            target_dir = AUDIOBOOKS_DIR if book.is_audiobook else BOOKS_DIR
            is_in_correct_dir = False
            try:
                src.resolve().relative_to(target_dir.resolve())
                is_in_correct_dir = True
            except ValueError:
                pass

            changed = (new_filename_with_folder != book.filename) or not is_in_correct_dir
            if not changed:
                results.append({"id": book.id, "original": book.filename, "new": new_filename_with_folder, "changed": False})
                continue
            if apply:
                try:
                    original_filename = book.filename
                    old_parent = src.parent
                    
                    # Ensure book is moved to correct base directory first
                    current, base_dir = _ensure_correct_base_dir(book, src)
                    
                    if scheme != "original":
                        current, new_rel = renamer.rename_book_file(current, base_dir, scheme, meta, custom_tpl)
                        book.filename = new_rel
                    if folder_mode != "flat":
                        current, new_rel = renamer.organize_into_folders(
                            current, base_dir, book.author,
                            getattr(book, "series", None), folder_mode
                        )
                        book.filename = new_rel
                    _cleanup_empty_dirs(old_parent)
                    _cleanup_empty_dirs(current.parent)
                    results.append({"id": book.id, "original": original_filename, "new": book.filename, "changed": True})
                except Exception as e:
                    errors.append({"id": book.id, "original": book.filename, "error": str(e)})
            else:
                results.append({"id": book.id, "original": book.filename, "new": new_filename_with_folder, "changed": True})
        if apply:
            db.session.commit()
        return jsonify({"results": results, "errors": errors, "applied": apply})

    @app.route("/api/library/clean-authors", methods=["POST"])
    @login_required
    def clean_authors():
        """Find any author formatted as "Lastname, Firstname" (exactly one comma) and swap them to "Firstname Lastname"."""
        books = Book.query.filter(Book.author.like("%,%")).all()
        updated = 0
        for book in books:
            author = book.author
            if author and author.count(",") == 1:
                parts = [p.strip() for p in author.split(",")]
                new_author = f"{parts[1]} {parts[0]}"
                book.author = new_author
                updated += 1
                file_path = get_book_path(book)
                if file_path.exists():
                    try:
                        _rename_and_organize(book, file_path)
                    except Exception as exc:
                        logger.warning("Rename after author clean failed for book %s: %s", book.id, exc)
        if updated > 0:
            db.session.commit()
        return jsonify({"success": True, "updated": updated})

    # -----------------------------------------------------------------------
    # Metadata
    # -----------------------------------------------------------------------

    @app.route("/api/books/<int:book_id>/fetch-metadata", methods=["POST"])
    @login_required
    def fetch_metadata(book_id):
        book = Book.query.get_or_404(book_id)
        data = request.get_json(silent=True) or {}
        source = data.get("source", "open_library")
        query = data.get("query") or book.isbn or book.isbn13 or book.title or ""
        apply_to_book = data.get("apply", False)

        try:
            results = scraper.search_all_sources(query)
            if apply_to_book and results:
                _apply_metadata(book, results[0])
            return jsonify(results)
        except Exception as exc:
            logger.exception("Failed to fetch metadata for book %s: %s", book_id, exc)
            return jsonify({"error": f"Failed to fetch metadata: {str(exc)}"}), 500

    @app.route("/api/books/<int:book_id>/apply-metadata", methods=["POST"])
    @login_required
    def apply_metadata(book_id):
        book = Book.query.get_or_404(book_id)
        meta = request.get_json(silent=True) or {}
        try:
            # Explicit user selection always replaces all fields
            _apply_metadata(book, meta, replace_missing_only=False)
            # Re-apply rename/organize after metadata import
            file_path = get_book_path(book)
            if file_path.exists():
                try:
                    _rename_and_organize(book, file_path)
                except Exception as exc:
                    logger.warning("Rename after metadata apply failed for book %s: %s", book_id, exc)
            return jsonify(book.to_dict())
        except Exception as exc:
            logger.exception("Failed to apply metadata for book %s: %s", book_id, exc)
            return jsonify({"error": f"Failed to apply metadata: {str(exc)}"}), 500

    @app.route("/api/metadata/search", methods=["GET"])
    @login_required
    def metadata_search():
        query = request.args.get("q", "").strip()
        if not query:
            return jsonify({"error": "Query required"}), 400

        # Determine which sources to search
        sources_param = request.args.get("sources", "")
        if sources_param:
            requested = [s.strip() for s in sources_param.split(",") if s.strip()]
        else:
            priority = Settings.get("source_priority", "")
            requested = [s.strip() for s in priority.split(",") if s.strip()] if priority else list(scraper.DEFAULT_SOURCE_ORDER)

        # Filter out disabled sources
        disabled_raw = Settings.get("sources_disabled", "")
        disabled = {s.strip() for s in disabled_raw.split(",") if s.strip()}
        sources = [s for s in requested if s not in disabled]

        return jsonify(scraper.search_all_sources(query, sources=sources))

    @app.route("/api/metadata/sources", methods=["GET"])
    @login_required
    def get_meta_sources():
        priority = Settings.get("source_priority", ",".join(scraper.DEFAULT_SOURCE_ORDER))
        disabled_raw = Settings.get("sources_disabled", "")
        disabled = {s.strip() for s in disabled_raw.split(",") if s.strip()}
        return jsonify({
            "all": scraper.DEFAULT_SOURCE_ORDER,
            "labels": scraper.SOURCE_LABELS,
            "priority": [s for s in priority.split(",") if s.strip()],
            "disabled": list(disabled),
        })

    @app.route("/api/metadata/sources", methods=["PUT"])
    @login_required
    def set_meta_sources():
        data = request.get_json(silent=True) or {}
        if "priority" in data:
            Settings.set("source_priority", ",".join(data["priority"]))
        if "disabled" in data:
            Settings.set("sources_disabled", ",".join(data["disabled"]))
        return jsonify({"ok": True})

    # -----------------------------------------------------------------------
    # Covers
    # -----------------------------------------------------------------------

    @app.route("/api/books/<int:book_id>/cover", methods=["GET"])
    @login_required
    def get_cover(book_id):
        thumb = request.args.get("thumb", "false").lower() == "true"
        path = cover_mgr.get_cover_path(book_id, thumb=thumb)
        if path:
            return send_file(str(path), mimetype="image/jpeg")
        abort(404)

    @app.route("/api/books/<int:book_id>/cover", methods=["POST"])
    @login_required
    def upload_cover(book_id):
        book = Book.query.get_or_404(book_id)
        file_obj = request.files.get("file") or request.files.get("cover")
        if file_obj:
            data = file_obj.read()
        else:
            body = request.get_json(silent=True) or {}
            url = body.get("url")
            if not url:
                return jsonify({"error": "No file or URL provided"}), 400
            data = scraper.fetch_cover_image(url)
            if not data:
                return jsonify({"error": "Could not download cover"}), 502

        cf = cover_mgr.save_cover(book_id, data)
        if not cf:
            return jsonify({"error": "Failed to process image"}), 500
        book.cover_filename = cf
        db.session.commit()

        # Auto-embed cover in EPUB
        if book.file_format == "epub":
            epub_path = str(get_book_path(book))
            cover_path = cover_mgr.get_cover_path(book_id)
            if cover_path:
                with open(str(cover_path), "rb") as fh:
                    cover_bytes = fh.read()
                cover_mgr.embed_cover_in_epub(epub_path, cover_bytes)

        return jsonify({"success": True, "cover_filename": cf})

    @app.route("/api/books/<int:book_id>/cover/embed", methods=["POST"])
    @login_required
    def embed_cover(book_id):
        book = Book.query.get_or_404(book_id)
        if book.file_format != "epub":
            return jsonify({"error": "Cover embedding only supported for EPUB files"}), 400
        path = cover_mgr.get_cover_path(book_id)
        if not path:
            return jsonify({"error": "No cover image found"}), 404
        with open(str(path), "rb") as f:
            data = f.read()
        success = cover_mgr.embed_cover_in_epub(str(get_book_path(book)), data)
        if success:
            return jsonify({"success": True})
        return jsonify({"error": "Failed to embed cover"}), 500

    @app.route("/api/books/<int:book_id>/cover", methods=["DELETE"])
    @login_required
    def delete_cover_route(book_id):
        book = Book.query.get_or_404(book_id)
        cover_mgr.delete_cover(book_id)
        book.cover_filename = None
        db.session.commit()
        return jsonify({"success": True})

    # -----------------------------------------------------------------------
    # Tags
    # -----------------------------------------------------------------------

    @app.route("/api/series", methods=["GET"])
    @login_required
    def list_series():
        rows = (
            db.session.query(Book.series)
            .filter(Book.series.isnot(None))
            .filter(Book.series != "")
            .distinct()
            .order_by(Book.series.asc())
            .all()
        )
        return jsonify([r[0] for r in rows])

    @app.route("/api/tags", methods=["GET"])
    @login_required
    def list_tags():
        # subqueryload fetches all book_tags in one extra query instead of N
        tags = Tag.query.options(subqueryload(Tag.book_tags)).order_by(Tag.name).all()
        return jsonify([t.to_dict() for t in tags])

    @app.route("/api/tags", methods=["POST"])
    @login_required
    def create_tag():
        data = request.get_json(silent=True) or {}
        name = (data.get("name") or "").strip()
        if not name:
            return jsonify({"error": "Name is required"}), 400
        existing = Tag.query.filter_by(name=name).first()
        if existing:
            return jsonify(existing.to_dict()), 200
        tag = Tag(name=name)
        db.session.add(tag)
        db.session.commit()
        return jsonify(tag.to_dict()), 201

    @app.route("/api/tags/<int:tag_id>", methods=["DELETE"])
    @login_required
    def delete_tag(tag_id):
        tag = Tag.query.get_or_404(tag_id)
        db.session.delete(tag)
        db.session.commit()
        return jsonify({"success": True})

    @app.route("/api/books/<int:book_id>/tags", methods=["GET"])
    @login_required
    def list_book_tags(book_id):
        Book.query.get_or_404(book_id)
        bts = BookTag.query.filter_by(book_id=book_id).all()
        return jsonify([bt.tag.to_dict() for bt in bts])

    @app.route("/api/books/<int:book_id>/tags", methods=["POST"])
    @login_required
    def add_book_tag(book_id):
        Book.query.get_or_404(book_id)
        data = request.get_json(silent=True) or {}
        name = (data.get("name") or "").strip()
        if not name:
            return jsonify({"error": "Tag name required"}), 400
        tag = Tag.query.filter_by(name=name).first()
        if not tag:
            tag = Tag(name=name)
            db.session.add(tag)
            db.session.flush()
        if not BookTag.query.filter_by(book_id=book_id, tag_id=tag.id).first():
            db.session.add(BookTag(book_id=book_id, tag_id=tag.id))
        db.session.commit()
        return jsonify(tag.to_dict()), 201

    @app.route("/api/books/<int:book_id>/tags/<int:tag_id>", methods=["DELETE"])
    @login_required
    def remove_book_tag(book_id, tag_id):
        bt = BookTag.query.filter_by(book_id=book_id, tag_id=tag_id).first_or_404()
        db.session.delete(bt)
        db.session.commit()
        return jsonify({"success": True})

    # -----------------------------------------------------------------------
    # Email Addresses
    # -----------------------------------------------------------------------

    @app.route("/api/email-addresses", methods=["GET"])
    @login_required
    def list_email_addresses():
        addresses = EmailAddress.query.order_by(EmailAddress.is_default.desc(), EmailAddress.label).all()
        return jsonify([a.to_dict() for a in addresses])

    @app.route("/api/email-addresses", methods=["POST"])
    @login_required
    def create_email_address():
        data = request.get_json(silent=True) or {}
        email = (data.get("email") or "").strip().lower()
        label = (data.get("label") or "").strip()
        if not email:
            return jsonify({"error": "Email is required"}), 400
        if not _valid_email(email):
            return jsonify({"error": "Invalid email address"}), 400
        if not label:
            label = email
        make_default = data.get("is_default", False)
        if make_default:
            EmailAddress.query.filter_by(is_default=True).update({"is_default": False})
        addr = EmailAddress(label=label, email=email, is_default=bool(make_default))
        db.session.add(addr)
        db.session.commit()
        return jsonify(addr.to_dict()), 201

    @app.route("/api/email-addresses/<int:addr_id>", methods=["PUT"])
    @login_required
    def update_email_address(addr_id):
        addr = EmailAddress.query.get_or_404(addr_id)
        data = request.get_json(silent=True) or {}
        if "label" in data:
            addr.label = (data["label"] or "").strip() or addr.email
        if "email" in data:
            addr.email = (data["email"] or "").strip().lower()
            if not _valid_email(addr.email):
                return jsonify({"error": "Invalid email address"}), 400
        if data.get("is_default"):
            EmailAddress.query.filter(EmailAddress.id != addr_id).update({"is_default": False})
            addr.is_default = True
        db.session.commit()
        return jsonify(addr.to_dict())

    @app.route("/api/email-addresses/<int:addr_id>", methods=["DELETE"])
    @login_required
    def delete_email_address(addr_id):
        addr = EmailAddress.query.get_or_404(addr_id)
        was_default = addr.is_default
        db.session.delete(addr)
        db.session.commit()
        # Promote oldest remaining address to default if we deleted the default
        if was_default:
            first = EmailAddress.query.order_by(EmailAddress.date_added).first()
            if first:
                first.is_default = True
                db.session.commit()
        return jsonify({"success": True})

    @app.route("/api/email-addresses/<int:addr_id>/set-default", methods=["POST"])
    @login_required
    def set_default_email(addr_id):
        EmailAddress.query.filter_by(is_default=True).update({"is_default": False})
        addr = EmailAddress.query.get_or_404(addr_id)
        addr.is_default = True
        db.session.commit()
        return jsonify(addr.to_dict())

    # -----------------------------------------------------------------------
    # Send via SMTP / Kindle
    # -----------------------------------------------------------------------

    @app.route("/api/books/<int:book_id>/send", methods=["POST"])
    @login_required
    def send_book(book_id):
        book = Book.query.get_or_404(book_id)
        data = request.get_json(silent=True) or {}

        # Resolve recipient: explicit > EmailAddress table default > legacy Settings key
        recipient = data.get("recipient")
        if not recipient:
            default_addr = EmailAddress.query.filter_by(is_default=True).first()
            recipient = default_addr.email if default_addr else Settings.get("kindle_email")
        if not recipient:
            return jsonify({"error": "No recipient email set. Add one in Settings → Account."}), 400

        smtp_host = Settings.get("smtp_host")
        smtp_port = _safe_int(Settings.get("smtp_port"), 587)
        smtp_user = Settings.get("smtp_user")
        smtp_password = crypto.decrypt_value(Settings.get("smtp_password") or "", DATA_DIR)
        use_tls = Settings.get("smtp_tls", "true").lower() == "true"
        sender_email = Settings.get("smtp_sender") or smtp_user

        if not smtp_host or not smtp_user or not smtp_password:
            return jsonify({"error": "SMTP settings incomplete. Configure in Settings first."}), 400

        filepath = str(get_book_path(book))
        ok, msg = mailer.send_book(
            filepath=filepath,
            recipient=recipient,
            smtp_host=smtp_host,
            smtp_port=smtp_port,
            smtp_user=smtp_user,
            smtp_password=smtp_password,
            use_tls=use_tls,
            sender_email=sender_email,
            subject=f"Book: {book.title or book.filename}",
        )
        if ok:
            return jsonify({"success": True, "message": msg})
        return jsonify({"error": msg}), 500

    # -----------------------------------------------------------------------
    # Settings
    # -----------------------------------------------------------------------

    SETTINGS_KEYS = [
        "smtp_host", "smtp_port", "smtp_user", "smtp_password",
        "smtp_tls", "smtp_sender",
        "auto_metadata", "meta_replace_missing", "apply_meta_cover",
        "source_priority", "sources_disabled", "folder_organization",
        "rename_scheme", "rename_custom_template",
        "display_name",
        "log_level",
    ]

    _MASKED = "••••••••"
    _MASKED_KEYS = {"smtp_password"}

    @app.route("/api/settings", methods=["GET"])
    @login_required
    def get_settings():
        result = {}
        for key in SETTINGS_KEYS:
            val = Settings.get(key)
            if key in _MASKED_KEYS:
                # Return bool-indicator instead of raw value
                result[key] = _MASKED if val else ""
            else:
                result[key] = val
        return jsonify(result)

    @app.route("/api/settings", methods=["PUT"])
    @login_required
    def update_settings():
        data = request.get_json(silent=True) or {}
        for key in SETTINGS_KEYS:
            if key not in data:
                continue
            val = data[key]
            # Skip masked placeholder writes
            if key in _MASKED_KEYS and val == _MASKED:
                continue
            if key in _MASKED_KEYS and val:
                val = crypto.encrypt_value(str(val), DATA_DIR)
            Settings.set(key, str(val) if val is not None else None)
        return jsonify({"success": True})

    @app.route("/api/settings/test-smtp", methods=["POST"])
    @login_required
    def test_smtp():
        data = request.get_json(silent=True) or {}
        host = data.get("smtp_host") or Settings.get("smtp_host")
        port = _safe_int(data.get("smtp_port") or Settings.get("smtp_port"), 587)
        user = data.get("smtp_user") or Settings.get("smtp_user")
        pwd = data.get("smtp_password")
        if not pwd or pwd == "••••••••":
            pwd = crypto.decrypt_value(Settings.get("smtp_password") or "", DATA_DIR)
        tls = str(data.get("use_tls", Settings.get("smtp_tls", "true"))).lower() == "true"
        if not host or not user or not pwd:
            return jsonify({"error": "Incomplete SMTP settings"}), 400
        ok, msg = mailer.test_smtp_connection(host, port, user, pwd, tls)
        if ok:
            return jsonify({"success": True, "message": msg})
        return jsonify({"success": False, "error": msg})

    @app.route("/api/settings/test-smtp-send", methods=["POST"])
    @login_required
    def test_smtp_send():
        data = request.get_json(silent=True) or {}
        host = data.get("smtp_host") or Settings.get("smtp_host")
        port = _safe_int(data.get("smtp_port") or Settings.get("smtp_port"), 587)
        user = data.get("smtp_user") or Settings.get("smtp_user")
        pwd = data.get("smtp_password")
        if not pwd or pwd == "••••••••":
            pwd = crypto.decrypt_value(Settings.get("smtp_password") or "", DATA_DIR)
        tls = str(data.get("use_tls", Settings.get("smtp_tls", "true"))).lower() == "true"
        recipient = data.get("recipient", "").strip()
        sender = data.get("sender_email") or Settings.get("smtp_sender") or user
        if not host or not user or not pwd:
            return jsonify({"error": "Incomplete SMTP settings"}), 400
        if not recipient:
            return jsonify({"error": "Recipient email is required"}), 400
        ok, msg = mailer.send_test_email(host, port, user, pwd, tls, recipient, sender)
        if ok:
            return jsonify({"success": True, "message": msg})
        return jsonify({"success": False, "error": msg})

    @app.route("/api/books/<int:book_id>/organize", methods=["POST"])
    @login_required
    def organize_book(book_id):
        """Move a book file into Author/ or Author/Series/ subfolder."""
        book = Book.query.get_or_404(book_id)
        src = get_book_path(book)
        if not src.exists():
            return jsonify({"error": "File not found on disk"}), 404
        
        # Ensure correct base directory first (self-healing migration)
        current, base_dir = _ensure_correct_base_dir(book, src)
        
        folder_mode = Settings.get("folder_organization", "flat")
        new_path, new_rel = renamer.organize_into_folders(
            current, base_dir, book.author, book.series, folder_mode
        )
        book.filename = new_rel
        db.session.commit()
        return jsonify({"success": True, "filename": new_rel})

    # -----------------------------------------------------------------------
    # Stats
    # -----------------------------------------------------------------------

    @app.route("/api/stats", methods=["GET"])
    @login_required
    def stats():
        total_books = Book.query.count()
        formats = db.session.query(Book.file_format, db.func.count()).group_by(Book.file_format).all()
        languages = (
            db.session.query(Book.language, db.func.count())
            .filter(Book.language.isnot(None))
            .group_by(Book.language)
            .all()
        )
        total_size = db.session.query(db.func.sum(Book.file_size)).scalar() or 0
        return jsonify({
            "total_books": total_books,
            "formats": {fmt: cnt for fmt, cnt in formats if fmt},
            "languages": {lang: cnt for lang, cnt in languages},
            "total_size_bytes": total_size,
        })

    @app.route("/api/logs", methods=["GET"])
    @login_required
    def get_logs():
        level = request.args.get("level", "").upper()
        lines = _log_buffer.get_lines()
        if level in ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"):
            level_names = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
            min_num = getattr(logging, level, logging.DEBUG)
            def _meets_level(line: str) -> bool:
                for lname in level_names:
                    if lname in line and getattr(logging, lname, 0) >= min_num:
                        return True
                return False
            lines = [l for l in lines if _meets_level(l)]
        return jsonify({"logs": lines})

    @app.route("/api/logs/level", methods=["PUT"])
    @login_required
    def set_log_level():
        data = request.get_json() or {}
        level = data.get("level", "INFO").upper()
        _valid = {"DEBUG", "INFO", "WARNING", "ERROR"}
        if level not in _valid:
            return jsonify({"error": f"Invalid level; must be one of {sorted(_valid)}"}), 400
        numeric = getattr(logging, level, logging.INFO)
        logging.getLogger().setLevel(numeric)
        Settings.set("log_level", level)
        return jsonify({"level": level})

    @app.route("/api/update-check", methods=["GET"])
    @login_required
    def update_check():
        return jsonify(_check_for_update())

    return app


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _rename_and_organize(book, file_path: Path):
    """Apply rename scheme and folder organization to a newly added book file."""
    scheme = Settings.get("rename_scheme", "original")
    custom_tpl = Settings.get("rename_custom_template", "")
    folder_mode = Settings.get("folder_organization", "flat")

    meta = {
        "title": book.title, "author": book.author,
        "published_date": book.published_date, "publisher": book.publisher,
        "isbn": book.isbn, "isbn13": book.isbn13, "language": book.language,
        "series": getattr(book, "series", None),
        "series_index": getattr(book, "series_order", None),
    }

    old_parent = file_path.parent
    
    # Ensure correct base directory first (self-healing migration)
    current, base_dir = _ensure_correct_base_dir(book, file_path)
    
    if scheme != "original":
        current, new_rel = renamer.rename_book_file(current, base_dir, scheme, meta, custom_tpl)
        book.filename = new_rel

    if folder_mode != "flat":
        current, new_rel = renamer.organize_into_folders(
            current, base_dir, book.author, getattr(book, "series", None), folder_mode
        )
        book.filename = new_rel

    # Clean up empty old parent directories (but never BOOKS_DIR or AUDIOBOOKS_DIR itself)
    if current != file_path:
        try:
            for folder in [old_parent, old_parent.parent]:
                if folder not in (BOOKS_DIR, AUDIOBOOKS_DIR) and folder.exists() and not any(folder.iterdir()):
                    folder.rmdir()
            # Also cleanup new hierarchy parents if anything empty there
            for folder in [current.parent, current.parent.parent]:
                if folder not in (BOOKS_DIR, AUDIOBOOKS_DIR) and folder.exists() and not any(folder.iterdir()):
                    folder.rmdir()
        except Exception:
            pass

    from models import db as _db
    _db.session.commit()


def _migrate_db(app):
    """Add columns introduced in newer schema versions to existing databases."""
    from sqlalchemy import text
    migrations = [
        "ALTER TABLE books ADD COLUMN series TEXT",
        "ALTER TABLE books ADD COLUMN series_order REAL",
        "CREATE TABLE IF NOT EXISTS tags (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL UNIQUE)",
        "CREATE TABLE IF NOT EXISTS book_tags (id INTEGER PRIMARY KEY AUTOINCREMENT, book_id INTEGER REFERENCES books(id), tag_id INTEGER REFERENCES tags(id), UNIQUE(book_id, tag_id))",
        "CREATE INDEX IF NOT EXISTS ix_books_language ON books (language)",
        "CREATE INDEX IF NOT EXISTS ix_books_series ON books (series)",
        "ALTER TABLE books ADD COLUMN read_status TEXT DEFAULT 'unread'",
        "ALTER TABLE books ADD COLUMN progress REAL DEFAULT 0.0",
        "ALTER TABLE books ADD COLUMN progress_location TEXT",
    ]
    with app.app_context():
        with db.engine.connect() as conn:
            for stmt in migrations:
                try:
                    conn.execute(text(stmt))
                    conn.commit()
                except Exception as exc:
                    logger.debug("Migration skipped (already applied): %s — %s", stmt[:60], exc)


def _fill_missing_audiobook_metadata(app):
    """Scan existing database for audiobooks and populate missing metadata/covers."""
    with app.app_context():
        # Get all audiobooks
        audiobooks = Book.query.filter_by(is_audiobook=True).all()
        updated = False
        for book in audiobooks:
            ext = Path(book.filename).suffix.lstrip(".").lower()
            
            # Ensure audio_format is set
            if not book.audio_format:
                book.audio_format = ext
                updated = True
                
            # If duration/narrator is missing, try to extract it
            if book.duration is None or book.narrator is None:
                path = get_book_path(book)
                if path.exists():
                    embedded = extract_embedded_metadata(path, ext)
                    if embedded:
                        _apply_metadata(book, embedded, replace_missing_only=True)
                        updated = True
                        
            # If cover is missing, try to extract it from the audio file
            if not book.cover_filename:
                path = get_book_path(book)
                if path.exists():
                    cover_data = cover_mgr.extract_cover_from_audio(str(path))
                    if cover_data:
                        cf = cover_mgr.save_cover(book.id, cover_data)
                        if cf:
                            book.cover_filename = cf
                            updated = True
        if updated:
            db.session.commit()


def _apply_metadata(book: Book, meta: dict, replace_missing_only: bool = None):
    if replace_missing_only is None:
        replace_missing_only = Settings.get("meta_replace_missing", "true") == "true"
    
    # Map all metadata fields supported by models.Book
    field_map = {
        "title": "title",
        "author": "author",
        "published_date": "published_date",
        "page_count": "page_count",
        "publisher": "publisher",
        "language": "language",
        "description": "description",
        "isbn": "isbn",
        "isbn13": "isbn13",
        "rating": "rating",
        "series": "series",
        "series_order": "series_order",
        "narrator": "narrator",
        "duration": "duration",
    }
    
    for src_key, model_key in field_map.items():
        val = meta.get(src_key)
        if val is not None:
            # Handle numeric/string conversions safely
            if src_key == "published_date":
                val = str(val)[:4]  # store year only
            elif src_key == "series_order":
                try:
                    val = float(val)
                except (ValueError, TypeError):
                    continue
            elif src_key == "duration":
                try:
                    val = int(float(val))
                except (ValueError, TypeError):
                    continue
            elif src_key == "page_count":
                try:
                    val = int(float(val))
                except (ValueError, TypeError):
                    continue
            elif src_key == "rating":
                try:
                    val = float(val)
                except (ValueError, TypeError):
                    continue
            
            if replace_missing_only and getattr(book, model_key, None):
                continue
            setattr(book, model_key, val)
            
    # Apply chapters if present
    chapters = meta.get("chapters")
    if chapters is not None:
        if not replace_missing_only or not book.chapters:
            import json
            book.chapters = json.dumps(chapters)
            
    cover_url = meta.get("cover_url")
    if cover_url:
        if not replace_missing_only or not book.cover_filename:
            data = scraper.fetch_cover_image(cover_url)
            if data:
                cf = cover_mgr.save_cover(book.id, data)
                if cf:
                    book.cover_filename = cf
    db.session.commit()


def _auto_fetch_metadata(book: Book):
    """Search online metadata sources to fill gaps in book record."""
    # Build the best possible query: prefer ISBN, then clean title + author
    if book.isbn13:
        query = book.isbn13
    elif book.isbn:
        query = book.isbn
    else:
        title = book.title or ""
        # Strip underscores/dashes that look like filenames, drop trailing year
        title = re.sub(r"[_]+", " ", title)
        title = re.sub(r"\s*[-–]\s*\d{4}\s*$", "", title)
        title = re.sub(r"\s+", " ", title).strip()
        query = " ".join(filter(None, [title, book.author]))

    if not query.strip():
        return

    # Get enabled sources from settings (same order + disabled list)
    disabled_raw = Settings.get("sources_disabled", "")
    disabled = {s.strip() for s in disabled_raw.split(",") if s.strip()}
    priority_raw = Settings.get("source_priority", "")
    sources = [
        s.strip()
        for s in (priority_raw.split(",") if priority_raw else scraper.DEFAULT_SOURCE_ORDER)
        if s.strip() and s.strip() not in disabled
    ]

    results = scraper.search_all_sources(query, sources=sources)
    if results:
        _apply_metadata(book, results[0])


if __name__ == "__main__":
    app = create_app()
    app.run(host="0.0.0.0", port=5000, debug=False)
