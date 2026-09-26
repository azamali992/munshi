"""Payment proofs: a photo or PDF (bank-transfer screenshot, JazzCash / Easypaisa receipt, cheque photo) stored with
the business and linked to the money entry it proves -- a customer receipt, an expense, a supplier payment, a salary
payment, a staff advance, a statutory challan -- and shown on that entry's approval card. OWNED BY STREAM E.
Schema: V10 (domain/migrations_attachments.py).

Frozen contracts (domain/accounts.py):
  * content type is SNIFFED from the bytes (magic numbers) and must be in ATTACHMENT_CONTENT_TYPES; SVG / HTML /
    anything else is refused; size <= ATTACHMENT_MAX_BYTES; the filename is display text only, never a path.
  * `entity` is a key of ATTACHMENT_ENTITIES and the entity must exist (or, for 'approval', be a pending card).
  * uploading is NOT a gated money write: store_attachment audits 'attachment_uploaded' (a SECONDARY_ACTION).
    Linking rides on the existing approval flow: a card's args carry attachment ids; when the approved write creates
    its entry it calls link_attachment in the SAME transaction. link_attachment audits 'attachment_linked'
    (SECONDARY, never mapped in eval/run_eval.ACTION_TOOL -- it must not consume the parent's approval).
  * who may read: attachment_visible() below (the route and the card renderer both call it): attachments:read (owner,
    clerk) sees all; any other role sees only attachments whose uploaded_by is their own user_id. Attachments linked
    to salary_payment / staff_advance / statutory_payment, or to a card for a payroll tool, are OWNER-ONLY (owner
    decision 2), whoever uploaded them.
  * the audit payload carries att_id, sha256, size and content type -- never the bytes, never the file name.

What is done to an upload (store_attachment), in order:
  1. size: empty refused; > ATTACHMENT_MAX_BYTES refused (the route has already cut the body off at the limit).
  2. type: sniffed from the magic bytes (JPEG, PNG, WebP, PDF; HEIC only when pillow-heif is installed, converted to
     JPEG). A file whose NAME or CLAIMED type says something else (a PNG called receipt.pdf, an HTML file called
     .jpg) is refused outright, as is any name ending .html/.svg/.js/...: nothing honest arrives like that.
  3. images are DECODED with only the sniffed decoder enabled, refused beyond MAX_PIXELS (decompression bombs), turned
     upright from EXIF orientation, capped at MAX_EDGE px, and RE-ENCODED from raw pixels -- so EXIF (GPS, device),
     XMP, ICC, text chunks and anything appended after the image data are gone. A 320 px JPEG thumbnail is made.
  4. PDFs are NOT re-encoded (no PDF writer here). A structural scan refuses encryption and every active or embedding
     feature: /JavaScript /JS /Launch /EmbeddedFile(s) /RichMedia /XFA /SubmitForm /ImportData /GoToE, with #xx
     name-escapes decoded and every FlateDecode stream inflated (bounded) and scanned too -- so an object stream
     cannot hide them. LIMITS: streams in other filters (LZW, ASCII85, ...) are not decoded; a PDF must start with
     %PDF- and end with %%EOF. The file is served as application/pdf with nosniff, never as a page of this origin.
  5. bytes are written atomically (temp file + fsync + os.replace) under the business's own files directory with a
     random name; the same bytes already stored for this business are reused (dedupe by sha256), and the same person
     sending the same bytes twice gets the SAME record back (`duplicate: True`) -- a double tap is not two proofs, and
     re-sending a proof already used for another payment is caught when it is linked."""
from __future__ import annotations

import hashlib
import io
import os
import re
import secrets
import tempfile
import unicodedata
import zlib
from pathlib import Path

from munshi.domain import accounts
from munshi.domain.models import now_iso
from munshi.domain.repository.base import NotFoundError, RepositoryBase, StateError, new_id

MAX_PIXELS = 40_000_000          # decoded pixels allowed (a 12 MP phone photo is 12 M); beyond is a decompression bomb
MAX_EDGE = 2560                  # stored images are capped to this many pixels on the long edge
THUMB_EDGE = 320
JPEG_QUALITY, THUMB_QUALITY = 85, 75
MAX_PROOFS_PER_RECORD = 5        # a card may carry at most this many proofs
PDF_INFLATE_BUDGET = 32 * 1024 * 1024
OWNER_ONLY_ENTITIES = frozenset({"salary_payment", "staff_advance", "statutory_payment"})    # owner decision 2
PENDING_PROOF_DAYS = 7

KIND = {"image/jpeg": "image", "image/png": "image", "image/webp": "image", "application/pdf": "pdf"}
EXT = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp", "application/pdf": "pdf"}
_HEIC = "image/heic"
_EXT_TYPE = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "jpe": "image/jpeg", "jfif": "image/jpeg", "png": "image/png",
             "webp": "image/webp", "pdf": "application/pdf", "heic": _HEIC, "heif": _HEIC}
_CLAIMED_TYPE = {"image/jpeg": "image/jpeg", "image/jpg": "image/jpeg", "image/pjpeg": "image/jpeg", "image/png": "image/png",
                 "image/webp": "image/webp", "application/pdf": "application/pdf", "image/heic": _HEIC, "image/heif": _HEIC}
_REFUSED_EXT = frozenset({"html", "htm", "xhtml", "shtml", "svg", "svgz", "xml", "js", "mjs", "hta", "php", "exe", "bat",
                          "cmd", "sh", "ps1", "zip", "rar", "7z", "gz", "tar", "apk", "jar"})
_REFUSED_CLAIMED = frozenset({"text/html", "application/xhtml+xml", "image/svg+xml", "text/xml", "application/xml",
                              "application/javascript", "text/javascript", "application/zip", "application/x-zip-compressed"})
_PIL_FORMAT = {"image/jpeg": "JPEG", "image/png": "PNG", "image/webp": "WEBP", _HEIC: "HEIF"}
_STORED_RE = re.compile(r"^\d{4}/\d{2}/[0-9a-f]{32}(\.thumb)?\.(jpg|png|webp|pdf)$")
_ENTITY_ID_RE = re.compile(r"^[A-Za-z0-9_:.\-]{1,80}$")
_ATT_ID_RE = re.compile(r"^ATT-[0-9A-F]{8}$")
_BIDI = dict.fromkeys(map(ord, "‎‏‪‫‬‭‮⁦⁧⁨⁩"), None)
_PDF_FORBIDDEN = re.compile(rb"/(JavaScript|JS|Launch|EmbeddedFiles?|RichMedia|XFA|SubmitForm|ImportData|GoToE|Encrypt)(?![A-Za-z0-9])")
_PDF_NAME_ESC = re.compile(rb"#([0-9A-Fa-f]{2})")


class AttachmentError(ValueError):
    """A refused upload or link. `code` is stable (the frontend switches on it); `status` is the HTTP status the
    route answers with. A ValueError, so any existing caller that catches domain errors handles it."""

    def __init__(self, code: str, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.code, self.message, self.status = code, message, status


class ProofsNotEnabled(AttachmentError, NotImplementedError):
    """V10 has not been applied to this file (it registers after V8 and V9): proofs are not available here yet."""

    def __init__(self) -> None:
        super().__init__("not_enabled", "Payment proofs are not enabled on this installation yet.", 503)


# ============================================================================ pure helpers (tested directly)
def sniff(data: bytes) -> str | None:
    """The content type the BYTES say, or None. Never looks at a name or a claimed type."""
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data[:5] == b"%PDF-":
        return "application/pdf"
    if data[4:8] == b"ftyp" and data[8:12] in (b"heic", b"heix", b"hevc", b"hevx", b"heim", b"heis", b"mif1", b"msf1"):
        return _HEIC
    return None


def _check_claims(kind: str | None, filename: str, claimed: str) -> None:
    name = (filename or "").replace("\\", "/").rsplit("/", 1)[-1]
    ext = name.rsplit(".", 1)[-1].lower().strip() if "." in name else ""
    claimed = (claimed or "").split(";", 1)[0].strip().lower()
    if ext in _REFUSED_EXT or claimed in _REFUSED_CLAIMED:
        raise AttachmentError("unsupported_type", "Only a photo (JPEG, PNG, WebP) or a PDF can be attached.", 415)
    if kind is None:
        raise AttachmentError("unsupported_type", "Only a photo (JPEG, PNG, WebP) or a PDF can be attached.", 415)
    said = {t for t in (_EXT_TYPE.get(ext), _CLAIMED_TYPE.get(claimed)) if t}
    if said and said != {kind}:
        raise AttachmentError("type_mismatch", "The file's name or type does not match its contents; it was refused.", 415)


def clean_filename(name: str, content_type: str) -> str:
    """Display text only: the last path segment, NFKC, no control / bidi-override characters, no path or shell
    characters, at most 80 characters, and the extension of the SNIFFED type."""
    base = re.split(r"[\\/]", name or "")[-1]
    base = unicodedata.normalize("NFKC", base).translate(_BIDI)
    base = "".join(ch for ch in base if not unicodedata.category(ch).startswith("C"))
    base = re.sub(r"[^\w .()\-]", "_", base)
    base = re.sub(r"\s+", " ", base).strip(" ._")
    stem = base.rsplit(".", 1)[0] if "." in base else base
    stem = stem[:80].strip(" ._-") or "proof"
    return f"{stem}.{EXT[content_type]}"


def _heif_available() -> bool:
    try:
        import pillow_heif  # type: ignore[import-not-found]
        pillow_heif.register_heif_opener()
        return True
    except Exception:
        return False


def process_image(data: bytes, kind: str) -> tuple[bytes, str, int, int, bytes]:
    """Decode (only the sniffed decoder), cap, strip, re-encode. Returns (stored bytes, stored type, w, h, thumb)."""
    from PIL import Image, ImageOps

    if kind == _HEIC and not _heif_available():
        raise AttachmentError("heic_unsupported", "iPhone HEIC photos can't be read here yet: send a screenshot, or set "
                                                  "the camera to 'Most Compatible' (JPEG).", 415)
    out_type = "image/jpeg" if kind == _HEIC else kind
    try:
        # the size is read from the header BEFORE any pixel is decoded; MAX_PIXELS is below Pillow's own bomb limits
        # (no process-global warnings filter needed, which would not be thread-safe under concurrent uploads)
        im = Image.open(io.BytesIO(data), formats=[_PIL_FORMAT[kind]])
        w, h = im.size
        if w < 1 or h < 1 or w * h > MAX_PIXELS:
            raise AttachmentError("image_too_large", "The picture has too many pixels.", 413)
        im.seek(0)                       # animated WebP / APNG: the first frame only
        im.load()
    except AttachmentError:
        raise
    except Image.DecompressionBombError:
        raise AttachmentError("image_too_large", "The picture has too many pixels.", 413) from None
    except Exception:
        raise AttachmentError("bad_image", "The picture could not be read; it may be damaged.", 415) from None
    try:
        im = ImageOps.exif_transpose(im) or im
    except Exception:
        pass
    alpha = im.mode in ("RGBA", "LA", "PA") or (im.mode == "P" and "transparency" in im.info)
    mode = "L" if im.mode in ("1", "L", "I;16", "I") else ("RGBA" if alpha and out_type != "image/jpeg" else "RGB")
    im = im.convert(mode)
    if max(im.size) > MAX_EDGE:
        im.thumbnail((MAX_EDGE, MAX_EDGE), Image.Resampling.LANCZOS)
    clean = Image.frombytes(im.mode, im.size, im.tobytes())     # raw pixels only: no EXIF/XMP/ICC/text survives

    def encode(img) -> bytes:
        buf = io.BytesIO()
        if out_type == "image/jpeg":
            img.save(buf, "JPEG", quality=JPEG_QUALITY, optimize=True)
        elif out_type == "image/png":
            img.save(buf, "PNG", optimize=True)
        else:
            img.save(buf, "WEBP", quality=JPEG_QUALITY)
        return buf.getvalue()

    stored = encode(clean)
    for _ in range(4):               # a re-encode can outgrow the limit (rare): shrink until it fits
        if len(stored) <= accounts.ATTACHMENT_MAX_BYTES:
            break
        clean = clean.resize((max(1, clean.width * 3 // 4), max(1, clean.height * 3 // 4)), Image.Resampling.LANCZOS)
        stored = encode(clean)
    else:
        if len(stored) > accounts.ATTACHMENT_MAX_BYTES:
            raise AttachmentError("too_large", "The picture is too large even after compressing it.", 413)
    thumb = clean.copy()
    thumb.thumbnail((THUMB_EDGE, THUMB_EDGE), Image.Resampling.LANCZOS)
    if thumb.mode in ("RGBA", "LA"):
        bg = Image.new("RGB", thumb.size, (255, 255, 255))
        bg.paste(thumb, mask=thumb.getchannel("A"))
        thumb = bg
    elif thumb.mode != "RGB":
        thumb = thumb.convert("RGB")
    tb = io.BytesIO()
    thumb.save(tb, "JPEG", quality=THUMB_QUALITY, optimize=True)
    return stored, out_type, clean.width, clean.height, tb.getvalue()


def check_pdf(data: bytes) -> None:
    """Refuse a PDF that is malformed at the edges, encrypted, or carries any active / embedding feature (see the
    module docstring for what is and is not covered)."""
    if not data.startswith(b"%PDF-") or b"%%EOF" not in data[-2048:]:
        raise AttachmentError("bad_pdf", "The PDF could not be read; it may be damaged.", 415)
    chunks = [data]
    budget = PDF_INFLATE_BUDGET
    for m in re.finditer(rb"stream\r?\n", data):
        if budget <= 0:
            raise AttachmentError("unsafe_pdf", "The PDF is too complex to check, so it was refused.", 422)
        start = m.end()
        end = data.find(b"endstream", start)
        if end < 0:
            break
        try:
            d = zlib.decompressobj()
            out = d.decompress(data[start:end], budget)
            if d.unconsumed_tail:            # would inflate past the budget: a zip bomb or an absurd document
                raise AttachmentError("unsafe_pdf", "The PDF is too complex to check, so it was refused.", 422)
            budget -= len(out)
            chunks.append(out)
        except zlib.error:
            continue                         # not Flate (an image, or another filter): scanned raw above
    for chunk in chunks:
        norm = _PDF_NAME_ESC.sub(lambda m: bytes([int(m.group(1), 16)]), chunk)
        hit = _PDF_FORBIDDEN.search(norm)
        if hit:
            if hit.group(1) == b"Encrypt":
                raise AttachmentError("encrypted_pdf", "A password-protected PDF can't be attached: send a screenshot instead.", 422)
            raise AttachmentError("unsafe_pdf", "This PDF contains scripts or embedded files, so it was refused: send a "
                                                "screenshot of the receipt instead.", 422)


def _sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def _write_atomic(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".up-", suffix=".tmp")     # 0600 on POSIX
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# ============================================================================ the mixin
class AttachmentsMixin(RepositoryBase):
    # ------------------------------------------------------------ storage location
    def _files_root(self) -> Path:
        """<data>/files/<business_id> for a hub file <data>/tenants/<business_id>.db; <dir>/files/<stem> for any other
        file; a private temp dir for an in-memory repository. Never under web/static; never served statically."""
        root = getattr(self, "_att_root", None)
        if root is None:
            if self.db_path == ":memory:":
                root = Path(tempfile.mkdtemp(prefix="munshi-proofs-"))
            else:
                p = Path(self.db_path).resolve()
                base = p.parent.parent if p.parent.name == "tenants" else p.parent
                root = base / "files" / p.stem
            self._att_root = root
        return root

    def _stored_path(self, stored_name: str) -> Path:
        if not _STORED_RE.match(stored_name or ""):
            raise StateError("stored proof name is malformed")
        root = self._files_root().resolve()
        path = (root / stored_name).resolve()
        if not path.is_relative_to(root):
            raise StateError("stored proof path escapes the business's directory")
        return path

    def _proofs_ready(self) -> None:
        if not self._one("SELECT 1 FROM sqlite_master WHERE type='table' AND name='attachments'"):
            raise ProofsNotEnabled()

    # ------------------------------------------------------------ shapes
    def _att_row(self, att_id: str):
        self._proofs_ready()
        r = self._one("SELECT * FROM attachments WHERE att_id=?", (att_id,)) if _ATT_ID_RE.match(att_id or "") else None
        if not r:
            raise NotFoundError(f"no such attachment: {att_id}")
        return r

    @staticmethod
    def _meta(r) -> dict:
        return {"att_id": r["att_id"], "kind": KIND[r["content_type"]], "content_type": r["content_type"],
                "size_bytes": r["size_bytes"], "width": r["width"], "height": r["height"], "filename": r["filename"],
                "sha256": r["sha256"], "has_thumb": bool(r["thumb_name"]), "uploaded_by": r["uploaded_by"],
                "uploaded_by_name": r["uploaded_by_name"], "uploaded_at": r["uploaded_at"], "status": r["status"],
                "entity": r["entity"], "entity_id": r["entity_id"], "linked_by": r["linked_by"], "linked_at": r["linked_at"],
                "duplicate": False}

    def _links(self, att_id: str) -> list[dict]:
        return [dict(x) for x in self._all("SELECT entity, entity_id, approval_id, linked_by, linked_at FROM attachment_links "
                                           "WHERE att_id=? ORDER BY link_id", (att_id,))]

    def _owner_only(self, links: list[dict]) -> bool:
        for ln in links:
            if ln["entity"] in OWNER_ONLY_ENTITIES:
                return True
            if ln["entity"] == "approval":
                a = self._one("SELECT tool FROM approvals WHERE approval_id=?", (ln["entity_id"],))
                if a and accounts.TOOL_PERMISSION.get(a["tool"] or "", "").startswith("payroll:"):
                    return True
        return False

    # ------------------------------------------------------------ store
    def store_attachment(self, data: bytes, filename: str, content_type: str, uploaded_by: str, actor: str,
                         uploaded_by_name: str = "") -> dict:
        """Validate (sniffed type, size), clean and store; returns the metadata (see _meta; `duplicate` True when this
        uploader already sent exactly these bytes -- the earlier record is returned, nothing new is written)."""
        self._proofs_ready()
        if not uploaded_by:
            raise AttachmentError("bad_request", "An upload needs the uploader's user id.")
        data = bytes(data or b"")
        if not data:
            raise AttachmentError("empty", "The file is empty.")
        if len(data) > accounts.ATTACHMENT_MAX_BYTES:
            raise AttachmentError("too_large", f"A proof can be at most {accounts.ATTACHMENT_MAX_BYTES // (1024 * 1024)} MB.", 413)
        kind = sniff(data)
        _check_claims(kind, filename, content_type)
        source_sha = _sha(data)
        again = self._one("SELECT * FROM attachments WHERE source_sha256=? AND uploaded_by=? ORDER BY uploaded_at, att_id LIMIT 1",
                          (source_sha, uploaded_by))
        if again:
            return self._meta(again) | {"duplicate": True}
        if kind == "application/pdf":
            check_pdf(data)
            stored, stored_type, w, h, thumb = data, kind, None, None, b""
        else:
            stored, stored_type, w, h, thumb = process_image(data, kind)
        sha = _sha(stored)
        display = clean_filename(filename, stored_type)
        now = now_iso()
        att_id = new_att_id()
        written: list[Path] = []
        try:
            with self._tx() as c:
                again = c.execute("SELECT * FROM attachments WHERE source_sha256=? AND uploaded_by=? LIMIT 1",
                                  (source_sha, uploaded_by)).fetchone()
                if again:                            # a concurrent double tap won the race
                    return self._meta(again) | {"duplicate": True}
                stored_name = thumb_name = None
                for r in c.execute("SELECT stored_name, thumb_name FROM attachments WHERE sha256=? ORDER BY uploaded_at", (sha,)).fetchall():
                    p = self._stored_path(r["stored_name"])
                    if p.is_file() and _sha(p.read_bytes()) == sha:     # dedupe: this business already has these bytes
                        stored_name, thumb_name = r["stored_name"], r["thumb_name"]
                        break
                if stored_name is None:
                    stem = f"{now[:4]}/{now[5:7]}/{secrets.token_hex(16)}"
                    stored_name = f"{stem}.{EXT[stored_type]}"
                    _write_atomic(self._stored_path(stored_name), stored); written.append(self._stored_path(stored_name))
                    if thumb:
                        thumb_name = f"{stem}.thumb.jpg"
                        _write_atomic(self._stored_path(thumb_name), thumb); written.append(self._stored_path(thumb_name))
                c.execute("INSERT INTO attachments (att_id, sha256, source_sha256, stored_name, thumb_name, filename, content_type, "
                          "size_bytes, source_size, width, height, uploaded_by, uploaded_by_name, uploaded_at, status) "
                          "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,'pending')",
                          (att_id, sha, source_sha, stored_name, thumb_name, display, stored_type, len(stored), len(data),
                           w, h, uploaded_by, uploaded_by_name or self._current_user() or "", now))
                self.audit(actor or "api", "attachment_uploaded", "attachment", att_id,
                           {"att_id": att_id, "sha256": sha, "size_bytes": len(stored), "content_type": stored_type,
                            "source_size": len(data)}, user=uploaded_by_name or "")
        except BaseException:
            for p in written:                        # the row never landed: don't leave an orphan behind
                try:
                    p.unlink()
                except OSError:
                    pass
            raise
        return self._meta(self._att_row(att_id))

    # ------------------------------------------------------------ read
    def get_attachment(self, att_id: str) -> dict:
        """Metadata (no bytes) + its links + owner_only. NotFoundError if unknown (or not a well-formed id)."""
        r = self._att_row(att_id)
        links = self._links(att_id)
        return self._meta(r) | {"links": links, "owner_only": self._owner_only(links)}

    def attachment_bytes(self, att_id: str, variant: str = "file") -> bytes:
        """The stored bytes ('file') or the JPEG thumbnail ('thumb'). The file is checked against its sha256 on every
        read: a file changed on disk is never served."""
        r = self._att_row(att_id)
        if variant == "thumb":
            if not r["thumb_name"]:
                raise NotFoundError(f"{att_id} has no thumbnail")
            p = self._stored_path(r["thumb_name"])
            if not p.is_file():
                raise NotFoundError(f"{att_id} has no thumbnail")
            return p.read_bytes()
        if variant != "file":
            raise NotFoundError(f"no such variant: {variant}")
        p = self._stored_path(r["stored_name"])
        if not p.is_file():
            raise StateError(f"the stored file of {att_id} is missing")
        b = p.read_bytes()
        if _sha(b) != r["sha256"]:
            raise StateError(f"the stored file of {att_id} failed its integrity check")
        return b

    def attachment_visible(self, att: dict, user_id: str, *, read_all: bool, payroll: bool) -> bool:
        """THE read rule. `att` is get_attachment()'s dict. read_all = principal.can('attachments:read');
        payroll = principal.can('payroll:read'). Proofs of pay are owner-only whoever uploaded them; otherwise
        owner/clerk see all and everyone else only their own uploads."""
        if att.get("owner_only") and not payroll:
            return False
        return bool(read_all or (user_id and att["uploaded_by"] == user_id))

    def attachments_for(self, entity: str, entity_id: str) -> list[dict]:
        """Metadata of every attachment linked to the entry (entity 'approval' = a card), oldest link first."""
        self._proofs_ready()
        if entity not in accounts.ATTACHMENT_ENTITIES:
            raise AttachmentError("unknown_entity", f"proofs can't be attached to {entity!r}")
        ids = [r["att_id"] for r in self._all("SELECT att_id FROM attachment_links WHERE entity=? AND entity_id=? ORDER BY link_id",
                                              (entity, entity_id))]
        return [self.get_attachment(i) for i in ids]

    def pending_proofs(self, user_id: str, limit: int = 20) -> list[dict]:
        """This person's recent uploads not yet linked to a record (for the composer's 'attach' tray), newest first.
        Proofs already on a pending card are marked with `on_card`."""
        self._proofs_ready()
        rows = self._all("SELECT * FROM attachments WHERE uploaded_by=? AND status='pending' AND uploaded_at >= "
                         "strftime('%Y-%m-%dT%H:%M:%S+00:00', 'now', ?) ORDER BY uploaded_at DESC, att_id LIMIT ?",
                         (user_id, f"-{PENDING_PROOF_DAYS} days", max(1, min(int(limit), 100))))
        out = []
        for r in rows:
            card = self._pending_card_of(r["att_id"])
            out.append(self._meta(r) | {"on_card": card})
        return out

    def uploads_since(self, user_id: str, since_iso: str) -> int:
        """How many proofs this person uploaded since `since_iso` (the route's daily quota)."""
        self._proofs_ready()
        return int(self._one("SELECT COUNT(*) n FROM attachments WHERE uploaded_by=? AND uploaded_at >= ?", (user_id, since_iso))["n"])

    def _pending_card_of(self, att_id: str) -> str | None:
        r = self._one("SELECT l.entity_id FROM attachment_links l JOIN approvals a ON a.approval_id = l.entity_id "
                      "WHERE l.att_id=? AND l.entity='approval' AND a.status='pending' ORDER BY l.link_id DESC LIMIT 1", (att_id,))
        return r["entity_id"] if r else None

    # ------------------------------------------------------------ cards (Stream D)
    def validate_proofs_for_card(self, att_ids: list[str], user_id: str, *, read_all: bool = False) -> list[dict]:
        """Before a card is raised with proof ids: every id must exist, be the requester's own upload (unless the
        requester has attachments:read), and still be free (not linked to a record, not on another pending card).
        Returns their metadata in the given order. Refusals are AttachmentError (code not_found / already_linked /
        too_many); a foreign id answers not_found, exactly like an unknown one."""
        ids = list(dict.fromkeys(att_ids or []))
        if len(ids) > MAX_PROOFS_PER_RECORD:
            raise AttachmentError("too_many", f"At most {MAX_PROOFS_PER_RECORD} proofs per entry.")
        out = []
        for att_id in ids:
            try:
                a = self.get_attachment(att_id)
            except NotFoundError:
                raise AttachmentError("not_found", f"No such proof: {att_id}", 404) from None
            if not read_all and a["uploaded_by"] != user_id:
                raise AttachmentError("not_found", f"No such proof: {att_id}", 404)
            if a["status"] == "linked":
                raise AttachmentError("already_linked", f"{att_id} is already the proof of {a['entity_id']}.", 409)
            if self._pending_card_of(att_id):
                raise AttachmentError("already_linked", f"{att_id} is already on another card waiting for approval.", 409)
            out.append(a)
        return out

    # ------------------------------------------------------------ link
    def _entity_exists(self, c, entity: str, entity_id: str) -> None:
        table = accounts.ATTACHMENT_ENTITIES[entity]
        if not re.fullmatch(r"[a-z_]+", table) or not c.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone():
            raise AttachmentError("entity_not_found", f"No {entity} records exist on this installation yet.", 404)
        if entity == "approval":
            r = c.execute("SELECT status FROM approvals WHERE approval_id=?", (entity_id,)).fetchone()
            if not r:
                raise AttachmentError("entity_not_found", f"No such card: {entity_id}", 404)
            if r["status"] != "pending":
                raise AttachmentError("approval_not_pending", f"Card {entity_id} is already decided.", 409)
            return
        pk = [row["name"] for row in c.execute(f"PRAGMA table_info({table})").fetchall() if row["pk"] == 1]
        col = pk[0] if len(pk) == 1 else "rowid"
        if not c.execute(f"SELECT 1 FROM {table} WHERE {col}=?", (entity_id,)).fetchone():
            raise AttachmentError("entity_not_found", f"No such {entity}: {entity_id}", 404)

    def link_attachment(self, att_id: str, entity: str, entity_id: str, actor: str, approval_id: str | None = None) -> dict:
        """Link a proof to a record. Call it INSIDE the approved write's transaction (it joins it) so the record and
        its proof commit or roll back together. Idempotent for the same (att_id, entity, entity_id): the second call
        writes nothing and returns created=False. Refuses (AttachmentError):
          unknown_entity / entity_not_found / approval_not_pending   -- the target isn't there (or the card is decided)
          already_linked   -- the proof already proves a DIFFERENT record from a different approved action, or sits on
                              another pending card. Several records from ONE approved action (a salary batch paid by
                              one transfer) may share a proof: pass approval_id, or link the card first ('approval',
                              card id) and the card id is inferred.
        Writes one 'attachment_linked' audit row (SECONDARY_ACTIONS) per new link."""
        if entity not in accounts.ATTACHMENT_ENTITIES:
            raise AttachmentError("unknown_entity", f"proofs can't be attached to {entity!r}")
        if not isinstance(entity_id, str) or not _ENTITY_ID_RE.match(entity_id):
            raise AttachmentError("entity_not_found", "That record id is not valid.", 404)
        with self._tx() as c:
            r = self._att_row(att_id)
            same = c.execute("SELECT entity, entity_id, approval_id, linked_by, linked_at FROM attachment_links "
                             "WHERE att_id=? AND entity=? AND entity_id=?", (att_id, entity, entity_id)).fetchone()
            if same:
                return self.get_attachment(att_id) | {"created": False, "link": dict(same)}
            self._entity_exists(c, entity, entity_id)
            links = self._links(att_id)
            finals = [ln for ln in links if ln["entity"] != "approval"]
            if entity == "approval":
                if finals:
                    raise AttachmentError("already_linked", f"{att_id} is already the proof of {finals[0]['entity_id']}.", 409)
                card = self._pending_card_of(att_id)
                if card and card != entity_id:
                    raise AttachmentError("already_linked", f"{att_id} is already on card {card}, waiting for approval.", 409)
            else:
                if approval_id is None:
                    cards = [ln["entity_id"] for ln in links if ln["entity"] == "approval"]
                    approval_id = cards[-1] if cards else None
                if finals and not (approval_id and all(ln["approval_id"] == approval_id for ln in finals)):
                    raise AttachmentError("already_linked", f"{att_id} is already the proof of {finals[0]['entity_id']}; "
                                                            "one proof can't prove two different payments.", 409)
            who = self._current_user() or actor or ""
            now = now_iso()
            c.execute("INSERT INTO attachment_links (att_id, entity, entity_id, approval_id, linked_by, linked_at) VALUES (?,?,?,?,?,?)",
                      (att_id, entity, entity_id, approval_id if entity != "approval" else entity_id, who, now))
            if entity != "approval" and r["status"] == "pending":
                c.execute("UPDATE attachments SET status='linked', entity=?, entity_id=?, linked_by=?, linked_at=? WHERE att_id=?",
                          (entity, entity_id, who, now, att_id))
            self.audit(actor or "api", "attachment_linked", entity, entity_id,
                       {"att_id": att_id, "sha256": r["sha256"], "size_bytes": r["size_bytes"], "content_type": r["content_type"],
                        "entity": entity, "entity_id": entity_id, "approval_id": approval_id})
            link = c.execute("SELECT entity, entity_id, approval_id, linked_by, linked_at FROM attachment_links "
                             "WHERE att_id=? AND entity=? AND entity_id=?", (att_id, entity, entity_id)).fetchone()
        return self.get_attachment(att_id) | {"created": True, "link": dict(link)}


def new_att_id() -> str:
    return new_id(accounts.ID_PREFIX["attachment"])          # ATT-XXXXXXXX (random, uuid4)
