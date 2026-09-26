"""Payment proofs: a photo or PDF (bank-transfer screenshot, JazzCash / Easypaisa receipt, cheque photo) stored with
the business and linked to the money entry it proves -- a customer receipt, an expense, a supplier payment, a salary
payment, a staff advance, a statutory challan -- and shown on that entry's approval card. OWNED BY STREAM E.
Stream 0 skeleton: signatures raising NotImplementedError; schema is V10 (domain/migrations_attachments.py).

Frozen contracts (domain/accounts.py):
  * content type is SNIFFED from the bytes (magic numbers) and must be in ATTACHMENT_CONTENT_TYPES; SVG / HTML /
    anything else is refused; size <= ATTACHMENT_MAX_BYTES; the filename is display text only, never a path.
  * `entity` is a key of ATTACHMENT_ENTITIES and the entity must exist (or, for 'approval', be a pending card).
  * uploading is NOT a gated money write: store_attachment audits 'attachment_uploaded' (a SECONDARY_ACTION).
    Linking rides on the existing approval flow: a card's args carry attachment ids; when the approved write creates
    its entry it calls link_attachment in the SAME transaction. link_attachment audits 'attachment_linked'
    (SECONDARY, never mapped in eval/run_eval.ACTION_TOOL -- it must not consume the parent's approval).
  * who may read (enforced in web/routes/attachments.py, not here): attachments:read (owner, clerk) sees all; any
    other role sees only attachments whose uploaded_by is their own user_id. Attachments linked to salary_payment /
    staff_advance / statutory_payment are OWNER-ONLY (owner decision 2), whoever uploaded them -- except the employee
    whose own salary payment it proves, if Stream A exposes that on my_payslips.
  * the audit payload carries att_id, sha256, size and content type -- never the bytes."""
from __future__ import annotations

from munshi.domain.repository.base import RepositoryBase


def _todo(name: str):
    raise NotImplementedError(f"{name} is Stream E's (payment proofs); not implemented yet")


class AttachmentsMixin(RepositoryBase):
    def store_attachment(self, data: bytes, filename: str, content_type: str, uploaded_by: str, actor: str) -> dict:
        """Validate (sniffed type, size) and store; returns {att_id, sha256, content_type, size_bytes, filename, uploaded_by, uploaded_at}."""
        _todo("store_attachment")

    def get_attachment(self, att_id: str) -> dict:
        """Metadata (no bytes) + its links. NotFoundError if unknown."""
        _todo("get_attachment")

    def attachment_bytes(self, att_id: str) -> bytes:
        _todo("attachment_bytes")

    def link_attachment(self, att_id: str, entity: str, entity_id: str, actor: str) -> dict:
        """Link to an entry (idempotent for the same triple). Call inside the approved write's transaction."""
        _todo("link_attachment")

    def attachments_for(self, entity: str, entity_id: str) -> list[dict]:
        """Metadata of every attachment linked to the entry, oldest first."""
        _todo("attachments_for")
