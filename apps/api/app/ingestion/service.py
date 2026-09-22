"""The ingestion pipeline: upload -> parse -> map -> clean -> validate -> dedupe -> commit.

Every stage writes its state to the institution store so a job can pause for
human review and resume later, and so the import report can say exactly which
source row produced which canonical record.
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

from ..institution_data.models import CanonicalRecord, RecordLineage
from ..institution_data.store import InstitutionDataStore
from ..normalization.canonical import CANONICAL_ENTITIES, CanonicalEntity, FieldType
from ..normalization.cleaning import clean_record
from ..normalization.deduplication import find_duplicates
from ..normalization.mapping import MappingEngine, apply_mapping, header_signature
from ..normalization.validation import identifier_pattern, summarize_issues, validate_record
from ..storage.object_store import ObjectStore, build_object_key, safe_file_name
from .detector import detect_file_kind
from .models import ParseResult, ParsedTable, ParserError
from .registry import ParserRegistry

JOB_QUEUED = "queued"
JOB_PROCESSING = "processing"
JOB_NEEDS_REVIEW = "needs_review"
JOB_READY = "ready"
JOB_IMPORTED = "imported"
JOB_FAILED = "failed"

STAGE_PARSING = "parsing"
STAGE_MAPPING = "mapping"
STAGE_MAPPING_REVIEW = "mapping_review"
STAGE_NORMALIZING = "normalizing"
STAGE_DUPLICATE_REVIEW = "duplicate_review"
STAGE_READY = "ready"
STAGE_IMPORTING = "importing"
STAGE_DONE = "done"

logger = logging.getLogger(__name__)


class IngestionError(ValueError):
    """A job-level failure that is safe to show to the requesting user."""


class CommitError(IngestionError):
    """The import step failed; the job was returned to ``ready`` with the error recorded."""


@dataclass(slots=True)
class IngestionService:
    store: InstitutionDataStore
    objects: ObjectStore
    parsers: ParserRegistry
    mapping: MappingEngine = field(default_factory=MappingEngine)
    max_upload_bytes: int = 50_000_000
    max_rows: int = 50_000
    auto_commit: bool = True

    # ------------------------------------------------------------------ upload
    def upload(self, institution_id: str, principal_id: str, *, file_name: str, content: bytes, content_type: str = "application/octet-stream", entity_hint: str | None = None, options: Mapping[str, Any] | None = None) -> dict[str, Any]:
        if not content:
            raise IngestionError("the uploaded file is empty")
        if len(content) > self.max_upload_bytes:
            raise IngestionError(f"the uploaded file exceeds the {self.max_upload_bytes} byte limit")
        if entity_hint and entity_hint not in CANONICAL_ENTITIES:
            raise IngestionError(f"unknown entity: {entity_hint}")
        name = safe_file_name(file_name)
        kind = detect_file_kind(name, content, content_type)
        file_id = f"file-{uuid4().hex}"
        object_key = build_object_key(institution_id, "raw", file_id, name)
        stored = self.objects.put(object_key, content, content_type)
        self.store.record_file(
            institution_id, file_id=file_id, file_name=name, content_type=content_type, size_bytes=stored.size_bytes,
            sha256=stored.sha256, object_key=object_key, file_kind=kind.value, uploaded_by=principal_id,
        )
        job_id = f"job-{uuid4().hex}"
        job_options = dict(options or {})
        job_options.setdefault("auto_commit", self.auto_commit)
        return self.store.create_job(institution_id, job_id=job_id, file_id=file_id, entity=entity_hint, requested_by=principal_id, options=job_options)

    def create_job_from_parse_result(self, institution_id: str, principal_id: str, result: ParseResult, *, source_kind: str, entity_hint: str | None = None, options: Mapping[str, Any] | None = None) -> dict[str, Any]:
        """Jobs for pull sources (cloud sheets, existing databases) that never had a file."""

        job_id = f"job-{uuid4().hex}"
        job_options = dict(options or {})
        job_options.setdefault("auto_commit", self.auto_commit)
        job_options["source_label"] = result.file_name
        job = self.store.create_job(institution_id, job_id=job_id, file_id=None, entity=entity_hint, requested_by=principal_id, source_kind=source_kind, options=job_options)
        self._stage_parsed_rows(institution_id, job, result)
        return self.store.get_job(institution_id, job_id) or job

    # ----------------------------------------------------------------- process
    async def process(self, institution_id: str, job_id: str) -> dict[str, Any]:
        """Run the pipeline for a queued job.

        A job still ``processing`` was interrupted (a worker restart); it is
        restarted from the beginning, discarding whatever the interrupted run
        left behind, so the re-queued background job can pick it up again.
        """

        job = self._job(institution_id, job_id)
        if job["status"] not in {JOB_QUEUED, JOB_FAILED, JOB_PROCESSING}:
            return job
        if job["status"] == JOB_PROCESSING:
            job = self._reset_interrupted(institution_id, job)
        try:
            self.store.update_job(institution_id, job_id, status=JOB_PROCESSING, stage=STAGE_PARSING, error=None)
            if job.get("file_id"):
                result = self._parse_file(institution_id, job)
                job = self._stage_parsed_rows(institution_id, job, result)
            records = self.store.job_records(institution_id, job_id, limit=self.max_rows)
            if not records:
                raise IngestionError("no tabular rows were found; upload the file through the documents API if it is a policy or circular")
            return await self._map(institution_id, job, records)
        except CommitError:
            # commit() already recorded the error and returned the job to ready.
            return self._job(institution_id, job_id)
        except (IngestionError, ParserError, ValueError) as exc:
            return self.store.update_job(institution_id, job_id, status=JOB_FAILED, stage=job.get("stage", STAGE_PARSING), error=str(exc)[:500])
        except Exception as exc:  # noqa: BLE001 - a stuck job is worse than a generic failure
            logger.exception("ingestion job %s for institution %s failed unexpectedly", job_id, institution_id)
            message = f"the job failed unexpectedly ({type(exc).__name__}); the error has been logged for the operators"
            return self.store.update_job(institution_id, job_id, status=JOB_FAILED, stage=job.get("stage", STAGE_PARSING), error=message)

    def _reset_interrupted(self, institution_id: str, job: dict[str, Any]) -> dict[str, Any]:
        """Discard the partial state of an interrupted run before it is restarted.

        Review items the interrupted run created are closed (the restart will
        raise them again if they still apply). Rows are re-staged from the
        file when there is one; a pull-source job keeps its staged rows since
        they are the only copy of the source, and ``_normalize`` rebuilds
        their normalised state from ``raw``.
        """

        job_id = job["job_id"]
        for item in self.store.list_review_items(institution_id, job_id=job_id, status="pending"):
            self.store.resolve_review_item(institution_id, item["review_id"], status="rejected", resolved_by="system", resolution={"note": "superseded: the job was restarted after an interruption"})
        if job.get("file_id"):
            self.store.replace_job_records(institution_id, job_id, [])
        report = dict(job.get("report") or {})
        report.pop("normalization", None)
        report.pop("import", None)
        report["restarted"] = {"from_stage": job.get("stage"), "reason": "the previous run was interrupted"}
        return self.store.update_job(institution_id, job_id, report=report, mapping={})

    def _job(self, institution_id: str, job_id: str) -> dict[str, Any]:
        job = self.store.get_job(institution_id, job_id)
        if job is None:
            raise KeyError(f"ingestion job not found: {job_id}")
        return job

    def _parse_file(self, institution_id: str, job: dict[str, Any]) -> ParseResult:
        record = self.store.get_file(institution_id, job["file_id"])
        if record is None:
            raise IngestionError("the uploaded file record is missing")
        content = self.objects.get(record["object_key"])
        result = self.parsers.parse(record["file_name"], content, content_type=record["content_type"])
        return result

    def _select_table(self, job: dict[str, Any], result: ParseResult) -> ParsedTable:
        wanted = (job.get("options") or {}).get("sheet")
        candidates = [table for table in result.tables if table.headers and table.records]
        if wanted:
            for table in candidates:
                if table.name == wanted:
                    return table
            raise IngestionError(f"sheet or table not found: {wanted}")
        if not candidates:
            raise IngestionError("no tabular rows were found (no table with a header row was detected); upload the file through the documents API if it is a policy or circular")
        return max(candidates, key=lambda table: (table.row_count, len(table.headers)))

    def _stage_parsed_rows(self, institution_id: str, job: dict[str, Any], result: ParseResult) -> dict[str, Any]:
        table = self._select_table(job, result)
        rows = table.records[: self.max_rows]
        staged = [
            {
                "row_number": record.row_number,
                "locator": record.locator,
                "raw": {key: (value if isinstance(value, (str, int, float, bool)) or value is None else str(value)) for key, value in record.fields.items()},
                "normalized": {"_ocr": record.ocr, "_ocr_confidence": record.ocr_confidence},
                "status": "parsed",
                "action": "pending",
                "issues": [],
            }
            for record in rows
        ]
        self.store.replace_job_records(institution_id, job["job_id"], staged)
        report = dict(job.get("report") or {})
        report["parse"] = result.as_dict()
        report["selected_table"] = {"name": table.name, "headers": list(table.headers), "rows": len(rows), "truncated": table.row_count > len(rows), "warnings": list(table.warnings)}
        return self.store.update_job(institution_id, job["job_id"], row_count=len(rows), sheet_name=table.name, report=report, stage=STAGE_MAPPING)

    # ----------------------------------------------------------------- mapping
    async def _map(self, institution_id: str, job: dict[str, Any], records: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
        headers = list(job.get("report", {}).get("selected_table", {}).get("headers") or list(records[0]["raw"].keys()))
        samples = {header: [row["raw"].get(header) for row in records[:50]] for header in headers}
        entity_hint = job.get("entity") or None
        detected_entity: str | None = entity_hint
        if detected_entity is None:
            detected_entity, _, _ = self.mapping.detect_entity(headers, samples)
        signature = header_signature(headers)
        profile = self.store.find_mapping_profile(institution_id, detected_entity, signature)
        proposal = await self.mapping.propose(headers, samples, entity_hint=entity_hint or detected_entity, saved_profile=profile["mapping"] if profile else None)
        profile_incomplete = bool(profile) and bool(proposal.missing_required())
        if profile_incomplete:
            # The remembered profile no longer covers the entity's required fields:
            # propose afresh and let a human decide rather than importing nothing.
            proposal = await self.mapping.propose(headers, samples, entity_hint=entity_hint or detected_entity)
        mapping_state = proposal.as_dict()
        mapping_state["header_signature"] = signature
        mapping_state["profile_incomplete"] = profile_incomplete
        needs_review = bool(proposal.review_required() or proposal.missing_required())
        if needs_review:
            review_payload = {
                "entity": proposal.entity,
                "entity_confidence": mapping_state["entity_confidence"],
                "entity_alternatives": mapping_state["entity_alternatives"],
                "review_required": [item.as_dict() for item in proposal.review_required()],
                "missing_required": list(proposal.missing_required()),
                "unmapped": list(proposal.unmapped()),
                "proposed_mapping": {item.source_header: item.canonical_field for item in proposal.mappings},
                "headers": headers,
                "samples": {header: [str(value)[:60] for value in values[:3] if value not in (None, "")] for header, values in samples.items()},
            }
            self.store.add_review_items(institution_id, job["job_id"], [{"kind": "mapping", "payload": review_payload}])
            return self.store.update_job(institution_id, job["job_id"], status=JOB_NEEDS_REVIEW, stage=STAGE_MAPPING_REVIEW, entity=proposal.entity, mapping=mapping_state)
        job = self.store.update_job(institution_id, job["job_id"], entity=proposal.entity, mapping=mapping_state, stage=STAGE_NORMALIZING)
        return self._normalize(institution_id, job, proposal.mapped())

    async def apply_mapping(self, institution_id: str, job_id: str, *, mapping: Mapping[str, str | None], entity: str | None, approved_by: str, remember: bool = True) -> dict[str, Any]:
        """A reviewer confirms or corrects the proposed mapping; the job resumes."""

        job = self._job(institution_id, job_id)
        if job["status"] not in {JOB_NEEDS_REVIEW, JOB_FAILED} or job["stage"] not in {STAGE_MAPPING_REVIEW, STAGE_MAPPING, STAGE_NORMALIZING}:
            raise IngestionError("the job is not waiting for a mapping decision")
        entity_name = entity or job.get("entity")
        if not entity_name or entity_name not in CANONICAL_ENTITIES:
            raise IngestionError("a valid canonical entity is required")
        definition = CANONICAL_ENTITIES[entity_name]
        valid = set(definition.field_names())
        headers = list(job.get("report", {}).get("selected_table", {}).get("headers") or [])
        approved: dict[str, str] = {}
        for header, target in mapping.items():
            if target in (None, ""):
                continue
            if target not in valid:
                raise IngestionError(f"{target} is not a field of {entity_name}")
            if headers and header not in headers:
                raise IngestionError(f"unknown header: {header}")
            if target in approved.values():
                raise IngestionError(f"two headers were mapped to {target}")
            approved[header] = str(target)
        mapped_fields = set(approved.values())
        if "name" in valid and {"first_name", "last_name"} & mapped_fields:
            mapped_fields.add("name")
        missing = [name for name in definition.required_fields() if name not in mapped_fields]
        if missing:
            raise IngestionError(f"required fields are not mapped: {', '.join(missing)}")
        signature = job.get("mapping", {}).get("header_signature") or header_signature(headers)
        if remember:
            self.store.save_mapping_profile(institution_id, entity=entity_name, header_signature=signature, mapping=approved, approved_by=approved_by)
        for item in self.store.list_review_items(institution_id, job_id=job_id, status="pending"):
            if item["kind"] == "mapping":
                self.store.resolve_review_item(institution_id, item["review_id"], status="approved", resolved_by=approved_by, resolution={"mapping": approved, "entity": entity_name})
        mapping_state = dict(job.get("mapping") or {})
        mapping_state.update({"entity": entity_name, "approved_mapping": approved, "approved_by": approved_by, "header_signature": signature, "review_required": [], "missing_required": []})
        job = self.store.update_job(institution_id, job_id, status=JOB_PROCESSING, stage=STAGE_NORMALIZING, entity=entity_name, mapping=mapping_state, error=None)
        return self._normalize(institution_id, job, approved)

    # ---------------------------------------------------------- normalization
    def _normalize(self, institution_id: str, job: dict[str, Any], mapping: Mapping[str, str]) -> dict[str, Any]:
        entity = CANONICAL_ENTITIES[job["entity"]]
        staged = self.store.job_records(institution_id, job["job_id"], limit=self.max_rows)
        file_record = self.store.get_file(institution_id, job["file_id"]) if job.get("file_id") else None
        source_name = file_record["file_name"] if file_record else str((job.get("options") or {}).get("source_label") or job.get("source_kind"))
        source_file_id = file_record["file_id"] if file_record else None
        id_field = next((name for name in entity.natural_key if entity.field(name).field_type is FieldType.IDENTIFIER), None)
        id_values: list[str] = []
        prepared: list[tuple[dict[str, Any], CanonicalRecord]] = []
        for row in staged:
            canonical, extras = apply_mapping(entity, mapping, row["raw"])
            cleaned, notes, issues = clean_record(entity, canonical)
            if id_field and cleaned.get(id_field):
                id_values.append(str(cleaned[id_field]))
            lineage = RecordLineage(source_file_id=source_file_id, source_file_name=source_name, source_locator=row["locator"], ingestion_job_id=job["job_id"])
            prepared.append((row, CanonicalRecord(entity.name, cleaned, extras, tuple(notes), tuple(issues), lineage)))
        pattern = identifier_pattern(id_values) if id_values else None
        for row, record in prepared:
            ocr = bool(row["normalized"].get("_ocr")) if isinstance(row.get("normalized"), dict) else False
            confidence = row["normalized"].get("_ocr_confidence") if isinstance(row.get("normalized"), dict) else None
            record.issues = tuple(list(record.issues) + validate_record(entity, record.fields, ocr=ocr, ocr_confidence=confidence, id_pattern=pattern))
        records = [record for _, record in prepared]
        existing = self.store.existing_keys(institution_id, entity.name, [record.record_key for record in records if record.record_key.strip("|")])
        if entity.name in {"student", "faculty", "staff", "admission"}:
            people = self.store.lookup_person_matches(
                institution_id, entity.name,
                names=[record.fields.get("name") for record in records],
                phones=[record.fields.get("phone") for record in records],
                emails=[record.fields.get("email") for record in records],
            )
            for key, value in people.items():
                existing.setdefault(key, value)
        candidates, actions, duplicate_warnings = find_duplicates(records, existing)
        updated_rows: list[dict[str, Any]] = []
        for index, (row, record) in enumerate(prepared):
            action = actions.get(index, "insert")
            status = "ready"
            if record.has_blocking_issues():
                status = "rejected"
            elif action in {"conflict_in_batch", "duplicate_in_batch", "missing_key"}:
                status = "skipped" if action != "conflict_in_batch" else "review"
            updated_rows.append({
                "row_number": row["row_number"], "locator": row["locator"], "raw": row["raw"],
                "normalized": {"fields": record.fields, "attributes": record.attributes, "normalizations": list(record.normalizations)},
                "status": status, "action": action, "record_key": record.record_key, "issues": list(record.issues),
            })
        self.store.replace_job_records(institution_id, job["job_id"], updated_rows)
        review_items = []
        for candidate in candidates:
            if candidate.kind in {"conflicting_key", "probable_person"}:
                review_items.append({"kind": "duplicate", "payload": candidate.as_dict()})
        if review_items:
            self.store.add_review_items(institution_id, job["job_id"], review_items)
        issue_summary = summarize_issues([record.issues for record in records])
        actions_summary: dict[str, int] = {}
        for action in actions.values():
            actions_summary[action] = actions_summary.get(action, 0) + 1
        report = dict(job.get("report") or {})
        report["normalization"] = {
            "entity": entity.name,
            "rows": len(records),
            "rows_ready": sum(1 for item in updated_rows if item["status"] == "ready"),
            "rows_rejected": sum(1 for item in updated_rows if item["status"] == "rejected"),
            "rows_skipped": sum(1 for item in updated_rows if item["status"] == "skipped"),
            "rows_in_review": sum(1 for item in updated_rows if item["status"] == "review"),
            "planned_actions": actions_summary,
            "issues": issue_summary,
            "duplicate_candidates": [item.as_dict() for item in candidates[:100]],
            "normalizations_applied": sorted({note.split(":", 1)[1] if ":" in note else note for record in records for note in record.normalizations}),
            "identifier_pattern": pattern,
            "warnings": list(duplicate_warnings),
        }
        if review_items:
            return self.store.update_job(institution_id, job["job_id"], status=JOB_NEEDS_REVIEW, stage=STAGE_DUPLICATE_REVIEW, report=report)
        job = self.store.update_job(institution_id, job["job_id"], status=JOB_READY, stage=STAGE_READY, report=report)
        if (job.get("options") or {}).get("auto_commit"):
            return self.commit(institution_id, job["job_id"], committed_by=job["requested_by"])
        return job

    def resolve_review(self, institution_id: str, review_id: str, *, decision: str, resolved_by: str, note: str = "") -> dict[str, Any]:
        """Resolve a pending duplicate review item; mapping items go through ``apply_mapping``."""

        pending_item = next((entry for entry in self.store.list_review_items(institution_id, status="pending", limit=2000) if entry["review_id"] == review_id), None)
        if pending_item is None:
            raise KeyError(f"pending review item not found: {review_id}")
        if pending_item["kind"] != "duplicate":
            raise IngestionError("a mapping review is resolved by submitting the mapping decision, not by approving the item")
        item = self.store.resolve_review_item(institution_id, review_id, status=decision, resolved_by=resolved_by, resolution={"note": note})
        if item is None:
            raise KeyError(f"review item not found: {review_id}")
        job = self._job(institution_id, item["job_id"])
        pending = [entry for entry in self.store.list_review_items(institution_id, job_id=job["job_id"], status="pending") if entry["kind"] == "duplicate"]
        if job["stage"] == STAGE_DUPLICATE_REVIEW and not pending:
            job = self.store.update_job(institution_id, job["job_id"], status=JOB_READY, stage=STAGE_READY)
            if (job.get("options") or {}).get("auto_commit"):
                return self.commit(institution_id, job["job_id"], committed_by=resolved_by)
        return job

    # ------------------------------------------------------------------ commit
    def commit(self, institution_id: str, job_id: str, *, committed_by: str) -> dict[str, Any]:
        job = self._job(institution_id, job_id)
        if job["status"] not in {JOB_READY, JOB_NEEDS_REVIEW}:
            raise IngestionError(f"job cannot be committed from status {job['status']}")
        if job["stage"] not in {STAGE_READY, STAGE_DUPLICATE_REVIEW}:
            raise IngestionError(f"job cannot be committed from stage {job['stage']}")
        pending = self.store.list_review_items(institution_id, job_id=job_id, status="pending")
        if any(item["kind"] == "mapping" for item in pending):
            raise IngestionError("the mapping still needs review before the job can be committed")
        pending_duplicates = sum(1 for item in pending if item["kind"] == "duplicate")
        if pending_duplicates:
            raise IngestionError(f"{pending_duplicates} duplicate review item(s) are still pending; resolve them before committing")
        entity = CANONICAL_ENTITIES[job["entity"]]
        resolved = {item["review_id"]: item for item in self.store.list_review_items(institution_id, job_id=job_id, status=None)}
        skip_locators: set[str] = set()
        for item in resolved.values():
            if item["kind"] != "duplicate":
                continue
            payload = item.get("payload", {})
            if item["status"] == "pending":
                skip_locators.add(str(payload.get("left_locator")))
            elif item["status"] == "approved":
                # Reviewer confirmed it is the same record/person: the newer row is not imported.
                skip_locators.add(str(payload.get("left_locator")))
        self.store.update_job(institution_id, job_id, status=JOB_PROCESSING, stage=STAGE_IMPORTING, error=None)
        try:
            return self._import(institution_id, job, entity, skip_locators, committed_by=committed_by)
        except Exception as exc:  # noqa: BLE001 - the job must not stay in importing
            logger.exception("ingestion job %s for institution %s failed while importing", job_id, institution_id)
            message = f"the import failed ({type(exc).__name__}); the job can be committed again once the cause is fixed"
            self.store.update_job(institution_id, job_id, status=JOB_READY, stage=STAGE_READY, error=message)
            raise CommitError(message) from exc

    def _import(self, institution_id: str, job: dict[str, Any], entity: CanonicalEntity, skip_locators: set[str], *, committed_by: str) -> dict[str, Any]:
        job_id = job["job_id"]
        staged = self.store.job_records(institution_id, job_id, limit=self.max_rows)
        records: list[CanonicalRecord] = []
        skipped = 0
        for row in staged:
            if row["status"] != "ready" and row["status"] != "review":
                skipped += 1
                continue
            if row["locator"] in skip_locators:
                skipped += 1
                continue
            normalized = row.get("normalized") or {}
            lineage = RecordLineage(source_file_id=job.get("file_id"), source_file_name=str(job.get("report", {}).get("parse", {}).get("file_name") or (job.get("options") or {}).get("source_label") or ""), source_locator=row["locator"], ingestion_job_id=job_id)
            records.append(CanonicalRecord(entity.name, dict(normalized.get("fields", {})), dict(normalized.get("attributes", {})), tuple(normalized.get("normalizations", [])), tuple(row.get("issues", [])), lineage))
        if not records:
            report = dict(job.get("report") or {})
            report["import"] = {"entity": entity.name, "inserted": 0, "updated": 0, "unchanged": 0, "skipped": skipped, "committed_by": committed_by, "message": "no rows were eligible for import"}
            return self.store.update_job(institution_id, job_id, status=JOB_IMPORTED, stage=STAGE_DONE, report=report)
        summary = self.store.upsert_records(institution_id, records)
        summary.skipped += skipped
        report = dict(job.get("report") or {})
        import_report = summary.as_dict()
        import_report["committed_by"] = committed_by
        import_report["rows_considered"] = len(staged)
        report["import"] = import_report
        return self.store.update_job(institution_id, job_id, status=JOB_IMPORTED, stage=STAGE_DONE, report=report)

    # -------------------------------------------------------------- reporting
    def import_report(self, institution_id: str, job_id: str) -> dict[str, Any]:
        job = self._job(institution_id, job_id)
        report = dict(job.get("report") or {})
        report["job_id"] = job_id
        report["status"] = job["status"]
        report["stage"] = job["stage"]
        report["entity"] = job.get("entity")
        report["pending_reviews"] = len(self.store.list_review_items(institution_id, job_id=job_id, status="pending"))
        return report

    @staticmethod
    def sha256(content: bytes) -> str:
        return hashlib.sha256(content).hexdigest()

    @staticmethod
    def entity_definition(name: str) -> CanonicalEntity:
        return CANONICAL_ENTITIES[name]


__all__ = [
    "CommitError", "IngestionError", "IngestionService", "JOB_FAILED", "JOB_IMPORTED", "JOB_NEEDS_REVIEW", "JOB_PROCESSING", "JOB_QUEUED", "JOB_READY",
    "STAGE_DONE", "STAGE_DUPLICATE_REVIEW", "STAGE_MAPPING", "STAGE_MAPPING_REVIEW", "STAGE_NORMALIZING", "STAGE_PARSING", "STAGE_READY",
]
