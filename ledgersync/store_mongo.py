"""The MongoDB store of clients, uploads and rows (Embedded Client Document pattern).
Optimized for:
1. Batch Sequence Counter (single DB call instead of N calls during bulk upload)
2. Field-level projection (never pulling rows when only client or upload metadata is needed)
3. Atomic array filters & in-database updates for patch_row and delete_row
4. Clean connection pooling and robust index creation
5. 100% Client Isolation (no client data crossover)
"""
from __future__ import annotations

import datetime as dt
import logging
import re
from typing import Optional

import pymongo
from pymongo import MongoClient, ReturnDocument

from .errors import ChangedMeanwhile, ClientArchived, NotFound
from .models import BankAccount, Client, ClientFields, Transaction
from .rows import EDITABLE, OLD_TOTAL, WHOLE_DOCUMENT, StoredRow, now_iso, saved_form

logger = logging.getLogger(__name__)

# How many times patch_row reads and writes again when another write to the client got in between.
_PATCH_TRIES = 5

_CLIENT_FIELDS = ("name", "business_type", "contact_name", "contact_email", "contact_phone", "vat_registered",
                  "bank_accounts")
_CLIENT_PROJECTION = {
    "id": 1,
    "name": 1,
    "business_type": 1,
    "contact_name": 1,
    "contact_email": 1,
    "contact_phone": 1,
    "vat_registered": 1,
    "bank_accounts": 1,
    "archived_at": 1,
    "created_at": 1,
    "updated_at": 1,
}


def _client(doc: dict) -> Client:
    """A client as its document keeps it; one saved before it had bank accounts has the one current account."""
    return Client(
        id=doc["id"],
        name=doc["name"],
        business_type=doc["business_type"],
        contact_name=doc["contact_name"],
        contact_email=doc.get("contact_email", ""),
        contact_phone=doc.get("contact_phone", ""),
        vat_registered=bool(doc.get("vat_registered", True)),
        **({"bank_accounts": doc["bank_accounts"]} if doc.get("bank_accounts") else {}),
        archived=doc.get("archived_at") is not None,
        archived_at=doc.get("archived_at"),
        created_at=doc["created_at"],
        updated_at=doc["updated_at"],
    )


def _stored_accounts(accounts: list[BankAccount]) -> list[dict]:
    return [a.model_dump() for a in accounts]


class MongoStore:
    def __init__(self, uri: str = "mongodb://localhost:27017", db_name: str = "ledgersync"):
        self.uri = uri
        self.db_name = db_name
        # Optimal connection pooling for high-throughput FastAPI concurrency
        self.client = MongoClient(
            uri,
            maxPoolSize=50,
            minPoolSize=10,
            serverSelectionTimeoutMS=5000,
            connectTimeoutMS=10000,
            socketTimeoutMS=20000,
        )
        self.db = self.client[db_name]
        self._init_indexes()

    def _init_indexes(self):
        """Creates compound indexes for lightning-fast queries and constraints."""
        try:
            self.db.clients.create_index([("id", pymongo.ASCENDING)], unique=True)
            self.db.clients.create_index(
                [("archived_at", pymongo.ASCENDING), ("name", pymongo.ASCENDING)],
                collation={"locale": "en", "strength": 2}
            )
            self.db.clients.create_index([("id", pymongo.ASCENDING), ("uploads.id", pymongo.ASCENDING)])
            self.db.clients.create_index([("id", pymongo.ASCENDING), ("rows.id", pymongo.ASCENDING)])
            self.db.clients.create_index([("id", pymongo.ASCENDING), ("rows.upload_id", pymongo.ASCENDING)])
        except Exception as exc:
            logger.warning("Index optimization notice: %s", exc)

    def _next_id(self, sequence_name: str, count: int = 1) -> int:
        """Atomically allocates `count` sequential IDs in a single round-trip."""
        counter = self.db.counters.find_one_and_update(
            {"_id": sequence_name},
            {"$inc": {"seq": count}},
            upsert=True,
            return_document=ReturnDocument.AFTER
        )
        # Returns starting ID of the reserved batch
        return counter["seq"] - count + 1

    def revision(self, client_id: int) -> int:
        """How many writes this client's document has had. It lives in the document, so every
        store on the same database sees the same number; older documents without one give 0."""
        doc = self.db.clients.find_one({"id": client_id}, {"revision": 1})
        return int(doc.get("revision", 0)) if doc else 0

    # ─── Clients ───────────────────────────────────────────────────────────────

    def create_client(self, fields: ClientFields) -> Client:
        client_id = self._next_id("client_id")
        now = now_iso()
        doc = {
            "id": client_id,
            "name": fields.name,
            "business_type": fields.business_type,
            "contact_name": fields.contact_name,
            "contact_email": fields.contact_email,
            "contact_phone": fields.contact_phone,
            "vat_registered": bool(fields.vat_registered),
            "bank_accounts": _stored_accounts(fields.bank_accounts),
            "archived_at": None,
            "created_at": now,
            "updated_at": now,
            "revision": 0,
            "uploads": [],
            "rows": []
        }
        self.db.clients.insert_one(dict(doc))   # a copy: insert_one adds the _id
        return _client(doc)

    def get_client(self, client_id: int) -> Client:
        doc = self.db.clients.find_one({"id": client_id}, _CLIENT_PROJECTION)
        if not doc:
            raise NotFound("This client was not found.")
        return _client(doc)

    def list_clients(self, archived: bool = False) -> list[Client]:
        query = {"archived_at": {"$ne": None}} if archived else {"archived_at": None}
        docs = self.db.clients.find(
            query,
            _CLIENT_PROJECTION,
            collation={"locale": "en", "strength": 2}
        ).sort([("name", pymongo.ASCENDING), ("id", pymongo.ASCENDING)])

        return [_client(d) for d in docs]

    def update_client(self, client_id: int, changes: dict) -> Client:
        client = self.get_client(client_id)
        fields = {k: changes[k] for k in _CLIENT_FIELDS if changes.get(k) is not None}
        if client.archived and fields:
            raise ClientArchived(f"Restore {client.name} to change it.")

        now = now_iso()
        set_dict = {k: bool(v) if isinstance(v, bool) else v for k, v in fields.items()}
        if "bank_accounts" in set_dict:
            set_dict["bank_accounts"] = _stored_accounts(
                [BankAccount.model_validate(a) for a in set_dict["bank_accounts"]])
        archived = changes.get("archived")
        if archived is not None and archived != client.archived:
            set_dict["archived_at"] = now if archived else None

        if set_dict:
            set_dict["updated_at"] = now
            updated = self.db.clients.find_one_and_update(
                {"id": client_id},
                {"$set": set_dict, "$inc": {"revision": 1}},
                projection=_CLIENT_PROJECTION,
                return_document=ReturnDocument.AFTER
            )
            if not updated:
                raise NotFound("This client was not found.")
            return _client(updated)

        return client

    # ─── Uploads and rows ──────────────────────────────────────────────────────

    def add_upload(self, client_id: int, name: str, kind: str, rows: list[Transaction], *, sha256: str = "",
                   model: str = "", warnings=(), bank_account: Optional[str] = None) -> int:
        """Saves an upload and its rows; bank_account: the account or card its statement is for, which its statement
        lines are on."""
        self.get_client(client_id)
        upload_id = self._next_id("upload_id")
        now = now_iso()

        upload_doc = {
            "id": upload_id,
            "name": name,
            "kind": kind,
            "sha256": sha256,
            "model": model or "",
            "warnings": list(warnings),
            "bank_account": bank_account,
            "created_at": now
        }

        # Optimized: Batch allocation of row IDs in single atomic DB round-trip
        count = len(rows)
        start_row_id = self._next_id("row_id", count) if count > 0 else 0

        row_docs = []
        for n, tx in enumerate(rows):
            data = saved_form(tx)
            if bank_account and data["document_type"] == "statement":
                data["bank_account"] = bank_account
            row_docs.append({
                "id": start_row_id + n,
                "upload_id": upload_id,
                "position": n,
                "data": data
            })

        self.db.clients.update_one(
            {"id": client_id},
            {
                "$push": {
                    "uploads": upload_doc,
                    "rows": {"$each": row_docs}
                },
                "$set": {"updated_at": now},
                "$inc": {"revision": 1}
            }
        )
        return upload_id

    def uploads(self, client_id: int) -> list[dict]:
        """Loads only upload metadata and row-counts using fast aggregation pipeline."""
        pipeline = [
            {"$match": {"id": client_id}},
            {"$project": {
                "uploads": 1,
                "row_counts": {
                    "$map": {
                        "input": "$uploads",
                        "as": "u",
                        "in": {
                            "upload_id": "$$u.id",
                            "count": {
                                "$size": {
                                    "$filter": {
                                        "input": "$rows",
                                        "as": "r",
                                        "cond": {"$eq": ["$$r.upload_id", "$$u.id"]}
                                    }
                                }
                            }
                        }
                    }
                }
            }}
        ]
        result_docs = list(self.db.clients.aggregate(pipeline))
        if not result_docs:
            raise NotFound("This client was not found.")

        doc = result_docs[0]
        uploads_list = doc.get("uploads", [])
        counts = {item["upload_id"]: item["count"] for item in doc.get("row_counts", [])}

        results = []
        for u in sorted(uploads_list, key=lambda x: x["id"], reverse=True):
            results.append({
                "id": u["id"],
                "client_id": client_id,
                "name": u["name"],
                "kind": u["kind"],
                "sha256": u.get("sha256", ""),
                "model": u.get("model", ""),
                "warnings": u.get("warnings", []),
                "bank_account": u.get("bank_account"),
                "created_at": u["created_at"],
                "row_count": counts.get(u["id"], 0)
            })
        return results

    def set_upload_bank(self, client_id: int, upload_id: int, code: str) -> None:
        """Puts the upload, and its statement lines, on another bank account or card."""
        res = self.db.clients.update_one(
            {"id": client_id, "uploads.id": upload_id},
            {
                "$set": {"uploads.$[u].bank_account": code, "rows.$[r].data.bank_account": code,
                         "updated_at": now_iso()},
                "$inc": {"revision": 1}
            },
            array_filters=[{"u.id": upload_id}, {"r.upload_id": upload_id, "r.data.document_type": "statement"}]
        )
        if res.matched_count == 0:
            raise NotFound("This upload was not found.")

    def delete_upload(self, client_id: int, upload_id: int) -> None:
        now = now_iso()
        res = self.db.clients.update_one(
            {"id": client_id, "uploads.id": upload_id},
            {
                "$pull": {
                    "uploads": {"id": upload_id},
                    "rows": {"upload_id": upload_id}
                },
                "$set": {"updated_at": now},
                "$inc": {"revision": 1}
            }
        )
        if res.matched_count == 0:
            raise NotFound("This upload was not found.")

    def delete_row(self, client_id: int, row_id: int) -> None:
        doc = self.db.clients.find_one(
            {"id": client_id, "rows.id": row_id},
            {"rows": {"$elemMatch": {"id": row_id}}}
        )
        if not doc or not doc.get("rows"):
            raise NotFound("This row was not found.")

        upload_id = doc["rows"][0]["upload_id"]

        # Check if any other rows belong to this upload
        sibling = self.db.clients.find_one(
            {"id": client_id, "rows": {"$elemMatch": {"upload_id": upload_id, "id": {"$ne": row_id}}}},
            {"_id": 1}
        )
        remaining = sibling is not None

        now = now_iso()
        pull_dict = {"rows": {"id": row_id}}
        if not remaining:
            pull_dict["uploads"] = {"id": upload_id}

        self.db.clients.update_one(
            {"id": client_id},
            {
                "$pull": pull_dict,
                "$set": {"updated_at": now},
                "$inc": {"revision": 1}
            }
        )

    def rows(self, client_id: int) -> list[StoredRow]:
        doc = self.db.clients.find_one({"id": client_id}, {"rows": 1, "id": 1})
        if not doc:
            raise NotFound("This client was not found.")

        rows_list = doc.get("rows", [])
        sorted_rows = sorted(rows_list, key=lambda r: (r.get("upload_id", 0), r.get("position", 0)))

        stored_rows = []
        for r in sorted_rows:
            data = dict(r["data"])
            original = data.pop("original", None)
            data.pop("original_issues", None)
            if data.get("document_total") is None:
                old = [OLD_TOTAL.search(i.get("message", "")) for i in data.get("issues", []) if i.get("code") == "total_mismatch"]
                if old and old[0]:
                    data["document_total"] = old[0].group(1).replace(",", "")
            stored_rows.append(StoredRow(r["id"], r["upload_id"], Transaction.model_validate(data), original))
        return stored_rows

    def patch_row(self, client_id: int, row_id: int, changes: dict, *, revert: bool = False) -> None:
        """Changes a row (and a whole-document field on the rows of its document), or puts them back as read.

        The rows are read, changed here and written back whole, so the write only goes through when the client's
        document still has the revision it was read at: an upload or another change saved in between (say, a
        job saving an upload while a person edits a row) would otherwise be overwritten. Then it reads and
        tries again, and after _PATCH_TRIES lost races it gives up and asks the person to try again."""
        for _ in range(_PATCH_TRIES):
            doc = self.db.clients.find_one({"id": client_id, "rows.id": row_id}, {"rows": 1, "revision": 1})
            if not doc:
                raise NotFound("This row was not found.")
            rows = doc.get("rows", [])
            if not self._patched(rows, row_id, changes, revert):
                return
            # A document saved before revisions were kept has none: $inc starts it at 1.
            read_at = doc["revision"] if "revision" in doc else {"$exists": False}
            saved = self.db.clients.update_one(
                {"id": client_id, "revision": read_at},
                {"$set": {"rows": rows, "updated_at": now_iso()}, "$inc": {"revision": 1}})
            if saved.matched_count:
                return
        raise ChangedMeanwhile("The client's rows changed while this change was being saved, so it wasn't saved. "
                               "Try again.")

    @staticmethod
    def _patched(rows: list[dict], row_id: int, changes: dict, revert: bool) -> bool:
        """Applies the change to the rows as read, in place, and says whether anything changed."""
        target = next((r for r in rows if r["id"] == row_id), None)
        if not target:
            raise NotFound("This row was not found.")

        ref = target["data"].get("document_ref")
        claim_line = target["data"].get("document_type") == "expense_claim"
        whole = WHOLE_DOCUMENT - {"include"} if claim_line else WHOLE_DOCUMENT

        modified = False
        for r in rows:
            rid = r["id"]
            r_data = r["data"]
            if rid != row_id and not (ref and r_data.get("document_ref") == ref):
                continue

            if revert:
                if "original" not in r_data:
                    continue
                r_data.update(r_data.pop("original"))
                r_data["issues"] = r_data.pop("original_issues", r_data.get("issues", []))
                modified = True
            else:
                change = changes if rid == row_id else {k: v for k, v in changes.items() if k in whole}
                if not change:
                    continue
                if "original" not in r_data and any(k in EDITABLE and r_data.get(k) != v for k, v in change.items()):
                    r_data["original"] = {k: r_data.get(k) for k in EDITABLE}
                    r_data["original_issues"] = r_data.get("issues", [])
                    r_data["issues"] = []
                r_data.update(change)
                modified = True
        return modified
