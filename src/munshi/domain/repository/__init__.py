"""Munshi's own system of record. SQLite, standard library only, every
business invariant enforced here so that agents, the web app, evals and
tests all go through one door. Nothing in this package knows about LLMs.

Split by bounded context; `MunshiRepository` is the composition:
    base         connection, transactions, settings, audit, notifications, outbox, chat
    master       customers, products, suppliers, godowns, routes, vehicles, stock + moves
    orders       draft → confirmed → allocated → … → delivered/short/cancelled
    dispatch     plans, loading (stock leaves), stops closed by the customer's OTP
    cash         deposits + reconciliation, khata entries, office payments, expenses, purchases, payables, cashbook
    collections  aging (FIFO), reminders, promises
    reports      digest, sales, margin, stock ledger, valuation, slow stock, profit
    approvals    persisted human-in-the-loop approvals
    memory       learned names (per business, across conversations) and order habits
    office       the desktop office console: price changes + history, stock matrix, physical counts, client list
"""
from munshi.domain.repository.base import DOMAIN_ERRORS, CapacityError, CreditHoldError, InsufficientStockError, NotFoundError, OtpError, StateError, new_id
from munshi.domain.repository.cash import EXPENSE_CATEGORIES, PAYMENT_METHODS
from munshi.domain.repository.memory import MemoryMixin
from munshi.domain.repository.office import OfficeMixin


class MunshiRepository(OfficeMixin, MemoryMixin):
    """One business's data. Open with a file path, or ":memory:" for tests."""


__all__ = ["MunshiRepository", "NotFoundError", "InsufficientStockError", "CapacityError", "CreditHoldError", "OtpError",
           "StateError", "DOMAIN_ERRORS", "PAYMENT_METHODS", "EXPENSE_CATEGORIES", "new_id"]
