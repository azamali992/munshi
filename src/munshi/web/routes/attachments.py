"""Payment proofs: upload a photo / PDF, fetch it, list what an entry has. OWNED BY STREAM E. Stream 0: an empty
router, already mounted in web/app.py.

Contract (domain/accounts.py + domain/repository/attachments.py):
  POST /api/attachments               attachments:write (every role). Multipart; read at most ATTACHMENT_MAX_BYTES + 1
                                      bytes and refuse beyond (413) -- never trust Content-Length alone; the type is
                                      SNIFFED from the bytes. Rate-limit per principal. Returns metadata, never bytes.
  GET  /api/attachments/{att_id}      metadata + links. attachments:read (owner, clerk) for any; other roles only their
                                      own uploads (uploaded_by == principal.user_id), else 404 (not 403: don't confirm
                                      it exists). Proofs linked to salary_payment / staff_advance / statutory_payment:
                                      owner only (owner decision 2).
  GET  /api/attachments/{att_id}/file the bytes, same rule; Content-Type = the stored sniffed type; Content-Disposition
                                      inline with a sanitised filename; nosniff is already app-wide.
  Linking has no route of its own: it rides on the approved action (a card's args carry attachment ids)."""
from __future__ import annotations

from fastapi import APIRouter

router = APIRouter(tags=["attachments"])
