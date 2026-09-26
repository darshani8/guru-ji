"""The ingestion pipeline: upload -> parse -> map -> clean -> validate -> dedupe -> commit.

Every stage writes its state to the institution store so a job can pause for
human review and resume later, and so the import report can say exactly which
source row produced which canonical record.
"""

from __future__ import annotations

from datetime import datetime, timezone

import hashlib
import logging
import re
from collections import Counter
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any
from uuid import NAMESPACE_URL, uuid4, uuid5

from ..institution_data.models import CanonicalRecord, RecordLineage
from ..institution_data.store import InstitutionDataStore
from ..normalization.canonical import CANONICAL_ENTITIES, CanonicalEntity, FieldType
from ..normalization.cleaning import clean_record
from ..normalization.deduplication import find_duplicates
from ..normalization.mapping import MappingEngine, apply_mapping, header_signature, normalize_header
from ..normalization.validation import identifier_pattern, summarize_issues, validate_record
from ..storage.object_store import ObjectStore, build_object_key, safe_file_name
from .detector import detect_file_kind
from .models import FileKind, IntermediateRecord, ParseResult, ParsedTable, ParserError
from .registry import ParserRegistry
from .wide_marks import reshape_marks_sheets, reshaped_entity

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


class ProcessingError(IngestionError):
    """A server-side failure inside a stage; the job's state and the error were recorded and the stage can be retried."""


class CommitError(ProcessingError):
    """The import step failed; the job was returned to ``ready`` with the error recorded."""


# Paging for a job's review items: a large roster can raise thousands of them.
REVIEW_PAGE_SIZE = 2000


def _skip_reason(row: Mapping[str, Any]) -> str:
    """Why a staged row is not imported, named like the store's own skip reasons."""

    if row["status"] == "rejected":
        return "blocking_issues"
    if row.get("action") == "missing_key":
        return "missing_natural_key"
    return str(row.get("action") or row["status"])


def _nothing_importable(rows: Sequence[Mapping[str, Any]]) -> str:
    """Why no staged row can be imported, in words the uploader can act on."""

    rejected = [row for row in rows if row["status"] == "rejected"]
    errors: Counter[str] = Counter(
        f"{issue.get('field')} {issue.get('code')}" for row in rejected for issue in row.get("issues", []) if issue.get("severity") == "error"
    )
    no_key = sum(1 for row in rows if row["status"] != "rejected" and row.get("action") == "missing_key")
    reasons = []
    if rejected:
        detail = ", ".join(f"{name} ({count})" for name, count in errors.most_common(3))
        reasons.append(f"{len(rejected)} have errors" + (f": {detail}" if detail else ""))
    if no_key:
        reasons.append(f"{no_key} have no identifier")
    return (
        f"none of the {len(rows)} rows can be imported ({'; '.join(reasons) or 'no row is eligible'}); "
        "check the column mapping and the file, then retry the job to map it again"
    )[:500]


WORKBOOK_KINDS = {FileKind.XLSX, FileKind.XLS}
# The name grid_to_table gives a column whose header cell is blank, and the
# suffix dedupe_headers gives a repeated header ("Address (2)").
_UNNAMED_COLUMN = re.compile(r"column_\d+")
_REPEAT_SUFFIX = re.compile(r" \(\d+\)$")
# Header cells that read like values (an e-mail, a long number, a date).
# Headers of a row-number column ("Sl.No", "S. No", "Sr No", "#"). Only such a
# column counting 1, 2, 3... is discounted when deciding whether a sheet is a
# blank form; a data column that happens to count (Semester 1, 2, 3) still counts.
_SERIAL_HEADERS = frozenset({"sl", "sl no", "s no", "sr no", "si no", "sno", "slno", "srno", "serial no", "serial number", "no", "#"})
_VALUE_LIKE = re.compile(r"@|\d{6,}|\b\d{1,2}[./-]\d{1,2}[./-]\d{2,4}\b")


def _filled(value: Any) -> bool:
    return value is not None and str(value).strip() != ""


def _counts_rows(values: Sequence[Any]) -> bool:
    """A column holding only a running count (1, 2, 3...) numbers the rows; it is not data."""

    try:
        numbers = [float(str(value).strip()) for value in values]
    except ValueError:
        return False
    return all(number.is_integer() for number in numbers) and all(later == earlier + 1 for earlier, later in zip(numbers, numbers[1:]))


def _unusable_sheet(table: ParsedTable) -> str | None:
    """Why a sheet of a workbook has nothing to import, or ``None`` when it has."""

    if not table.headers:
        return "no header row was found (the sheet is empty or holds no table)"
    if not table.records:
        return "no data rows under the header"
    if sum(1 for cell in table.headers if _VALUE_LIKE.search(cell)) >= 2:
        return "the first row holds values (e-mail addresses, numbers or dates), so the sheet has no header row; add one and upload the sheet again"
    data_columns = 0
    for header in table.headers:
        values = [record.fields.get(header) for record in table.records if _filled(record.fields.get(header))]
        if values and not (normalize_header(header) in _SERIAL_HEADERS and _counts_rows(values)):
            data_columns += 1
            if data_columns == 2:
                return None
    return "fewer than two columns hold values (a blank form or a single list)"


def _sheet_columns(table: ParsedTable) -> frozenset[str]:
    """What makes two sheets "the same columns": the header names, compared the way a remembered
    mapping compares them (case, spacing and punctuation ignored); a repeated header and a blank,
    unnamed column do not make a sheet different."""

    filled = {header for record in table.records for header, value in record.fields.items() if _filled(value)}
    return frozenset(
        normalize_header(_REPEAT_SUFFIX.sub("", header)) for header in table.headers
        if header in filled or not _UNNAMED_COLUMN.fullmatch(header)
    )


def _merge_sheets(tables: Sequence[ParsedTable]) -> tuple[list[str], list[tuple[IntermediateRecord, dict[str, Any], int]]]:
    """One header list and one row list for sheets with the same columns.

    A header is kept under the first sheet's spelling of it ("Sl no" -> "Sl.no")
    and a column only some sheets have is added, never merged into another one.
    When several sheets are merged, each row gets a "Sheet" column naming its
    sheet. ``row_number`` orders the staged rows (the store sorts by it), so each
    sheet continues after the previous one; the locator keeps the sheet's own row.
    """

    headers: list[str] = []
    spelling: dict[str, str] = {}
    renames: list[dict[str, str]] = []
    for table in tables:
        rename: dict[str, str] = {}
        for header in table.headers:
            target = spelling.get(normalize_header(header), header)
            if target in rename.values():
                # Two columns of this sheet read alike: both are kept.
                target = header if header not in rename.values() else f"{header} ({table.name})"
            rename[header] = target
            if target not in headers:
                headers.append(target)
                spelling.setdefault(normalize_header(target), target)
        renames.append(rename)
    sheet_column = None
    if len(tables) > 1:
        sheet_column, number = "Sheet", 2
        while sheet_column in headers:
            sheet_column, number = f"Sheet ({number})", number + 1
        headers.append(sheet_column)
    rows: list[tuple[IntermediateRecord, dict[str, Any], int]] = []
    offset = 0
    for table, rename in zip(tables, renames):
        for record in table.records:
            fields = {rename.get(key, key): value for key, value in record.fields.items()}
            if sheet_column:
                fields[sheet_column] = table.name
            rows.append((record, fields, record.row_number + offset))
        offset += max((record.row_number for record in table.records), default=0)
    return headers, rows


@dataclass(slots=True)
class IngestionService:
    store: InstitutionDataStore
    objects: ObjectStore
    parsers: ParserRegistry
    mapping: MappingEngine = field(default_factory=MappingEngine)
    max_upload_bytes: int = 50_000_000
    max_rows: int = 50_000
    auto_commit: bool = True
    # A job left ``processing`` is restarted only when its last heartbeat is
    # older than this, so a run that is still alive is never duplicated.
    restart_after_seconds: int = 900

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
        self._stage_parsed_rows(institution_id, job, reshape_marks_sheets(result, entity=entity_hint))
        return self.store.get_job(institution_id, job_id) or job

    # ----------------------------------------------------------------- process
    async def process(self, institution_id: str, job_id: str, *, force: bool = False) -> dict[str, Any]:
        """Run the pipeline for a queued job.

        A job still ``processing`` whose heartbeat is stale was interrupted (a
        worker restart). If it had reached a step that is recorded before it
        runs (normalising under the mapping a reviewer approved, or
        importing), that step runs again, so nobody is asked for a decision
        twice; anything else is restarted from the beginning, discarding
        whatever the interrupted run left behind. A job whose heartbeat is
        fresh belongs to a run that is still alive and is returned unchanged
        unless ``force`` is set (as it is for a step the API hands to the job
        queue).
        """

        job = self._job(institution_id, job_id)
        if job["status"] not in {JOB_QUEUED, JOB_FAILED, JOB_PROCESSING}:
            return job
        # A job that failed after its mapping was chosen (no row importable, or
        # normalising broke) goes back to a person: the same mapping, remembered
        # or automatic, would only fail the same way again.
        remap = job["status"] == JOB_FAILED and job.get("stage") == STAGE_NORMALIZING
        previous_error = job.get("error") if remap else None
        mapping_state = dict(job.get("mapping") or {})
        if remap and mapping_state.get("approved_mapping") and mapping_state.pop("approval_unscheduled", False):
            # The reviewer's mapping was never applied (the queue refused it or
            # its worker died), so nothing says it is wrong: apply it now.
            job = self.store.update_job(institution_id, job_id, status=JOB_PROCESSING, mapping=mapping_state, error=None)
            resumed = self._resume(institution_id, job)
            if resumed is not None:
                return resumed
        if job["status"] == JOB_PROCESSING:
            if not force and not self._heartbeat_stale(job):
                return job
            resumed = self._resume(institution_id, job)
            if resumed is not None:
                return resumed
            job = self._reset_interrupted(institution_id, job)
        try:
            self.store.update_job(institution_id, job_id, status=JOB_PROCESSING, stage=STAGE_PARSING, error=None)
            if job.get("file_id"):
                result = self._parse_file(institution_id, job)
                job = self._stage_parsed_rows(institution_id, job, result)
            records = self.store.job_records(institution_id, job_id, limit=self.max_rows)
            if not records:
                raise IngestionError("no tabular rows were found; upload the file through the documents API if it is a policy or circular")
            return await self._map(institution_id, job, records, force_review=remap, previous_error=previous_error)
        except CommitError:
            # commit() already recorded the error and returned the job to ready.
            return self._job(institution_id, job_id)
        except (IngestionError, ParserError, ValueError) as exc:
            return self.store.update_job(institution_id, job_id, status=JOB_FAILED, stage=job.get("stage", STAGE_PARSING), error=str(exc)[:500])
        except Exception as exc:  # noqa: BLE001 - a stuck job is worse than a generic failure
            logger.exception("ingestion job %s for institution %s failed unexpectedly", job_id, institution_id)
            message = f"the job failed unexpectedly ({type(exc).__name__}); the error has been logged for the operators"
            return self.store.update_job(institution_id, job_id, status=JOB_FAILED, stage=job.get("stage", STAGE_PARSING), error=message)

    def _heartbeat_stale(self, job: Mapping[str, Any]) -> bool:
        try:
            last = datetime.fromisoformat(str(job.get("updated_at")))
        except (TypeError, ValueError):
            return True
        if last.tzinfo is None:
            last = last.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - last).total_seconds() > self.restart_after_seconds

    def _heartbeat(self, institution_id: str, job_id: str) -> None:
        """Refresh the job's timestamp so a concurrent restart knows this run is alive."""

        self.store.update_job(institution_id, job_id)

    def record_unscheduled(self, institution_id: str, job_id: str, error: str) -> dict[str, Any]:
        """The job queue refused the job's next step: record why, so an import can be committed again and anything else retried."""

        job = self._job(institution_id, job_id)
        if job["status"] not in {JOB_QUEUED, JOB_PROCESSING}:
            # The job already finished or failed on its own: that outcome stands.
            return job
        if job.get("stage") == STAGE_IMPORTING:
            return self.store.update_job(institution_id, job_id, status=JOB_READY, stage=STAGE_READY, error=error[:500])
        mapping_state = dict(job.get("mapping") or {})
        if job.get("stage") == STAGE_NORMALIZING and mapping_state.get("approved_mapping"):
            # The approved mapping was never applied (not scheduled, or its
            # worker kept dying): a retry applies it rather than asking again.
            mapping_state["approval_unscheduled"] = True
            return self.store.update_job(institution_id, job_id, status=JOB_FAILED, mapping=mapping_state, error=error[:500])
        return self.store.update_job(institution_id, job_id, status=JOB_FAILED, error=error[:500])

    def _resume(self, institution_id: str, job: dict[str, Any]) -> dict[str, Any] | None:
        """Carry on with the step a ``processing`` job recorded before it ran; ``None`` when it recorded none.

        Rows an interrupted import already brought in come back as unchanged.
        """

        job_id = job["job_id"]
        try:
            if job.get("stage") == STAGE_IMPORTING:
                return self.run_commit(institution_id, job_id)
            if job.get("stage") == STAGE_NORMALIZING and (job.get("mapping") or {}).get("approved_mapping"):
                return self.run_approved_mapping(institution_id, job_id)
        except ProcessingError:
            # The step recorded the failure on the job.
            return self._job(institution_id, job_id)
        return None

    def _close_pending_reviews(self, institution_id: str, job_id: str, *, note: str) -> None:
        for item in self._review_items(institution_id, job_id):
            if item["status"] == "pending":
                self.store.resolve_review_item(institution_id, item["review_id"], status="rejected", resolved_by="system", resolution={"note": note})

    def _reset_interrupted(self, institution_id: str, job: dict[str, Any]) -> dict[str, Any]:
        """Discard the partial state of an interrupted run before it is restarted.

        Review items the interrupted run created are closed (the restart will
        raise them again if they still apply). Rows are re-staged from the
        file when there is one; a pull-source job keeps its staged rows since
        they are the only copy of the source, and ``_normalize`` rebuilds
        their normalised state from ``raw``.
        """

        job_id = job["job_id"]
        self._close_pending_reviews(institution_id, job_id, note="superseded: the job was restarted after an interruption")
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
        # A wide marks sheet (one column per subject) is staged as one exam row per student, course and exam.
        return reshape_marks_sheets(result, entity=job.get("entity"))

    def _select_tables(self, job: dict[str, Any], result: ParseResult) -> tuple[list[ParsedTable], list[list[ParsedTable]], list[dict[str, str]]]:
        """The tables this job stages, the groups of sheets that become jobs of their own, and the sheets skipped (with why).

        An explicit sheet (or the sheets a job was split off with) is the only
        one taken. Otherwise every usable sheet of an uploaded workbook is:
        sheets with the same columns are merged, the first group in the
        workbook stays with this job and every other group gets its own job.
        Any other file keeps its largest table.
        """

        options = job.get("options") or {}
        wanted = options.get("sheet")
        candidates = [table for table in result.tables if table.headers and table.records]
        if wanted:
            for table in candidates:
                if table.name == wanted:
                    return [table], [], []
            raise IngestionError(f"sheet or table not found: {wanted}")
        if options.get("sheets"):
            found = {table.name: table for table in candidates}
            missing = [name for name in options["sheets"] if name not in found]
            if missing:
                raise IngestionError(f"sheet or table not found: {', '.join(missing)}")
            return [found[name] for name in options["sheets"]], [], []
        if result.file_kind not in WORKBOOK_KINDS or not job.get("file_id"):
            if not candidates:
                raise IngestionError("no tabular rows were found (no table with a header row was detected); upload the file through the documents API if it is a policy or circular")
            return [max(candidates, key=lambda table: (table.row_count, len(table.headers)))], [], []
        skipped = [{"sheet": warning.split(":", 1)[1], "reason": "the sheet could not be read"} for warning in result.warnings if warning.startswith("sheet_unreadable:")]
        groups: dict[frozenset[str], list[ParsedTable]] = {}
        for table in result.tables:
            reason = _unusable_sheet(table)
            if reason:
                skipped.append({"sheet": table.name, "reason": reason})
            else:
                groups.setdefault(_sheet_columns(table), []).append(table)
        if not groups:
            detail = "; ".join(f"{item['sheet']}: {item['reason']}" for item in skipped)
            raise IngestionError(f"no sheet of the workbook has rows to import ({detail}); name the sheet on upload to import it anyway"[:500])
        first, *others = groups.values()
        return first, others, skipped

    def _split_off(self, institution_id: str, job: dict[str, Any], groups: Sequence[Sequence[ParsedTable]]) -> list[dict[str, Any]]:
        """A queued job for each other group of sheets, reading the same stored file.

        The job id follows from this job and the sheets, so a parse that runs
        again (a retry, a restart) finds the jobs it made before instead of
        making them twice. The job queue schedules them (``queued_siblings``).
        """

        made: list[dict[str, Any]] = []
        for group in groups:
            sheets = [table.name for table in group]
            sibling_id = f"job-{uuid5(NAMESPACE_URL, '|'.join([job['job_id'], *sheets])).hex}"
            if self.store.get_job(institution_id, sibling_id) is None:
                options = {key: value for key, value in (job.get("options") or {}).items() if key not in {"sheet", "sheets", "origin_job_id"}}
                options.update(sheets=sheets, origin_job_id=job["job_id"])
                self.store.create_job(institution_id, job_id=sibling_id, file_id=job["file_id"], entity=job.get("entity"), requested_by=job["requested_by"], source_kind=job.get("source_kind") or "upload", options=options)
            made.append({"job_id": sibling_id, "sheets": sheets, "rows": sum(table.row_count for table in group)})
        return made

    def queued_siblings(self, institution_id: str, job: Mapping[str, Any]) -> list[dict[str, Any]]:
        """The jobs split off this job's workbook that still wait for the job queue."""

        listed = ((job.get("report") or {}).get("sheets") or {}).get("other_jobs") or []
        siblings = [self.store.get_job(institution_id, str(item.get("job_id"))) for item in listed]
        return [sibling for sibling in siblings if sibling is not None and sibling["status"] == JOB_QUEUED]

    def _stage_parsed_rows(self, institution_id: str, job: dict[str, Any], result: ParseResult) -> dict[str, Any]:
        tables, others, skipped = self._select_tables(job, result)
        headers, merged = _merge_sheets(tables)
        rows = merged[: self.max_rows]
        staged = [
            {
                "row_number": row_number,
                "locator": record.locator,
                "raw": {key: (value if isinstance(value, (str, int, float, bool)) or value is None else str(value)) for key, value in fields.items()},
                "normalized": {"_ocr": record.ocr, "_ocr_confidence": record.ocr_confidence},
                "status": "parsed",
                "action": "pending",
                "issues": [],
            }
            for record, fields, row_number in rows
        ]
        self.store.replace_job_records(institution_id, job["job_id"], staged)
        report = dict(job.get("report") or {})
        report["parse"] = result.as_dict()
        name = ", ".join(table.name for table in tables)
        warnings = [warning for table in tables for warning in ([f"{table.name}: {item}" for item in table.warnings] if len(tables) > 1 else table.warnings)]
        report["selected_table"] = {"name": name, "headers": headers, "rows": len(rows), "truncated": len(merged) > len(rows), "warnings": warnings}
        if others or skipped or len(tables) > 1:
            # Which sheet went where, so no sheet of the workbook goes missing unexplained.
            report["sheets"] = {"this_job": [table.name for table in tables], "other_jobs": self._split_off(institution_id, job, others), "skipped": skipped}
        return self.store.update_job(institution_id, job["job_id"], row_count=len(rows), sheet_name=name, report=report, stage=STAGE_MAPPING)

    # ----------------------------------------------------------------- mapping
    async def _map(self, institution_id: str, job: dict[str, Any], records: Sequence[Mapping[str, Any]], *, force_review: bool = False, previous_error: str | None = None) -> dict[str, Any]:
        headers = list(job.get("report", {}).get("selected_table", {}).get("headers") or list(records[0]["raw"].keys()))
        samples = {header: [row["raw"].get(header) for row in records[:50]] for header in headers}
        entity_hint = job.get("entity") or reshaped_entity(headers)
        signature = header_signature(headers)
        if force_review:
            profile = None
        elif entity_hint:
            profile = self.store.find_mapping_profile(institution_id, entity_hint, signature)
        else:
            # A reviewer's earlier decision for these headers wins over detection,
            # including one that corrected the detected entity.
            profile = self.store.find_latest_mapping_profile(institution_id, signature)
        # Only the uploader's choice is passed as a hint: a detected entity keeps
        # its real confidence and alternatives, so a doubtful guess is reviewed.
        proposal = await self.mapping.propose(headers, samples, entity_hint=entity_hint or (profile["entity"] if profile else None), saved_profile=profile["mapping"] if profile else None)
        profile_incomplete = bool(profile) and bool(proposal.missing_required())
        if profile_incomplete:
            # The remembered profile no longer covers the entity's required fields:
            # propose afresh and let a human decide rather than importing nothing.
            proposal = await self.mapping.propose(headers, samples, entity_hint=entity_hint)
        mapping_state = proposal.as_dict()
        mapping_state["header_signature"] = signature
        mapping_state["profile_incomplete"] = profile_incomplete
        entity_uncertain = not entity_hint and not proposal.profile_applied and proposal.entity_confidence < self.mapping.threshold
        mapping_state["entity_uncertain"] = entity_uncertain
        needs_review = bool(force_review or proposal.review_required() or proposal.missing_required() or entity_uncertain)
        if needs_review:
            review_payload = {
                "entity": proposal.entity,
                "entity_confidence": mapping_state["entity_confidence"],
                "entity_alternatives": mapping_state["entity_alternatives"],
                "entity_uncertain": entity_uncertain,
                "review_required": [item.as_dict() for item in proposal.review_required()],
                "missing_required": list(proposal.missing_required()),
                "unmapped": list(proposal.unmapped()),
                "proposed_mapping": {item.source_header: item.canonical_field for item in proposal.mappings},
                "headers": headers,
                "samples": {header: [str(value)[:60] for value in values[:3] if value not in (None, "")] for header, values in samples.items()},
                "previous_error": previous_error,
            }
            self.store.add_review_items(institution_id, job["job_id"], [{"kind": "mapping", "payload": review_payload}])
            return self.store.update_job(institution_id, job["job_id"], status=JOB_NEEDS_REVIEW, stage=STAGE_MAPPING_REVIEW, entity=proposal.entity, mapping=mapping_state)
        job = self.store.update_job(institution_id, job["job_id"], entity=proposal.entity, mapping=mapping_state, stage=STAGE_NORMALIZING)
        return self._normalize(institution_id, job, proposal.mapped())

    async def apply_mapping(self, institution_id: str, job_id: str, *, mapping: Mapping[str, str | None], entity: str | None, approved_by: str, remember: bool = True) -> dict[str, Any]:
        """A reviewer confirms or corrects the proposed mapping; the job resumes.

        The API does this in two steps, so that only the checks run inside the
        request: ``approve_mapping`` there, ``run_approved_mapping`` on the job queue.
        """

        self.approve_mapping(institution_id, job_id, mapping=mapping, entity=entity, approved_by=approved_by, remember=remember)
        return self.run_approved_mapping(institution_id, job_id)

    def approve_mapping(self, institution_id: str, job_id: str, *, mapping: Mapping[str, str | None], entity: str | None, approved_by: str, remember: bool = True) -> dict[str, Any]:
        """Check a reviewer's mapping and record it; nothing is normalised yet.

        Everything the reviewer can correct is refused here. The job is left
        ``processing`` at ``normalizing`` with the approved mapping, which is
        where ``run_approved_mapping`` (or a restart after an interruption) starts.
        """

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
            holder = next((name for name, chosen in approved.items() if chosen == target), None)
            if holder is not None:
                raise IngestionError(f"two headers were mapped to {target} ({holder!r} and {header!r}); keep {target} on one of them and leave the other unmapped")
            approved[header] = str(target)
        mapped_fields = set(approved.values())
        if "name" in valid and {"first_name", "last_name"} & mapped_fields:
            mapped_fields.add("name")
        missing = [name for name in definition.required_fields() if name not in mapped_fields]
        if missing:
            raise IngestionError(f"required fields are not mapped: {', '.join(missing)}")
        signature = job.get("mapping", {}).get("header_signature") or header_signature(headers)
        for item in self.store.list_review_items(institution_id, job_id=job_id, status="pending"):
            if item["kind"] == "mapping":
                self.store.resolve_review_item(institution_id, item["review_id"], status="approved", resolved_by=approved_by, resolution={"mapping": approved, "entity": entity_name})
        mapping_state = dict(job.get("mapping") or {})
        mapping_state.pop("approval_unscheduled", None)
        mapping_state.update({"entity": entity_name, "approved_mapping": approved, "approved_by": approved_by, "remember": remember, "header_signature": signature, "review_required": [], "missing_required": [], "entity_uncertain": False})
        return self.store.update_job(institution_id, job_id, status=JOB_PROCESSING, stage=STAGE_NORMALIZING, entity=entity_name, mapping=mapping_state, error=None)

    def run_approved_mapping(self, institution_id: str, job_id: str) -> dict[str, Any]:
        """Normalise (and with auto-commit import) the rows under the mapping ``approve_mapping`` recorded."""

        job = self._job(institution_id, job_id)
        state = job.get("mapping") or {}
        approved = dict(state.get("approved_mapping") or {})
        entity_name, signature, approved_by = str(job.get("entity")), str(state.get("header_signature")), str(state.get("approved_by"))
        remember = bool(state.get("remember", True))
        remembered = False

        def remember_once() -> None:
            nonlocal remembered
            if remember and not remembered:
                remembered = True
                self._remember_mapping(institution_id, job_id, entity=entity_name, signature=signature, mapping=approved, approved_by=approved_by)

        try:
            # Questions an earlier, interrupted run raised are asked again if they still apply.
            self._close_pending_reviews(institution_id, job_id, note="superseded: the rows were normalised again")
            # Remembered before the auto-commit starts, so an import that is
            # interrupted (and resumed at importing) does not lose it.
            job = self._normalize(institution_id, job, approved, before_commit=remember_once)
        except CommitError:
            # The rows were fine and only the import failed; commit() already
            # returned the job to ready with the error.
            remember_once()
            raise
        except Exception as exc:  # noqa: BLE001 - a job must never stay processing after a stage fails
            logger.exception("ingestion job %s for institution %s failed while normalizing", job_id, institution_id)
            message = f"normalization failed unexpectedly ({type(exc).__name__}); retry the job or submit the mapping again once the cause is fixed"
            self.store.update_job(institution_id, job_id, status=JOB_FAILED, stage=STAGE_NORMALIZING, error=message)
            raise ProcessingError(message) from exc
        remember_once()
        return job

    def _remember_mapping(self, institution_id: str, job_id: str, *, entity: str, signature: str, mapping: Mapping[str, str], approved_by: str) -> None:
        """Keep an approved mapping for the next upload of these headers once it produced importable rows.

        A mapping under which no row could be imported is not remembered: it
        would skip review on every re-upload and import nothing again. Saving
        is best effort, since the job itself has already moved on.
        """

        normalization = (self._job(institution_id, job_id).get("report") or {}).get("normalization") or {}
        if not int(normalization.get("rows_ready") or 0) + int(normalization.get("rows_in_review") or 0):
            return
        try:
            self.store.save_mapping_profile(institution_id, entity=entity, header_signature=signature, mapping=mapping, approved_by=approved_by)
        except Exception:  # noqa: BLE001 - the import stands; only the remembered mapping is missing
            logger.exception("could not remember the approved mapping of ingestion job %s for institution %s", job_id, institution_id)

    # ---------------------------------------------------------- normalization
    def _normalize(self, institution_id: str, job: dict[str, Any], mapping: Mapping[str, str], *, before_commit: Callable[[], None] | None = None) -> dict[str, Any]:
        entity = CANONICAL_ENTITIES[job["entity"]]
        staged = self.store.job_records(institution_id, job["job_id"], limit=self.max_rows)
        file_record = self.store.get_file(institution_id, job["file_id"]) if job.get("file_id") else None
        source_name = file_record["file_name"] if file_record else str((job.get("options") or {}).get("source_label") or job.get("source_kind"))
        source_file_id = file_record["file_id"] if file_record else None
        id_field = next((name for name in entity.natural_key if entity.field(name).field_type is FieldType.IDENTIFIER), None)
        id_values: list[str] = []
        prepared: list[tuple[dict[str, Any], CanonicalRecord]] = []
        for position, row in enumerate(staged):
            if position % 500 == 0:
                self._heartbeat(institution_id, job["job_id"])
            canonical, extras = apply_mapping(entity, mapping, row["raw"])
            cleaned, notes, issues = clean_record(entity, canonical)
            if id_field and cleaned.get(id_field):
                id_values.append(str(cleaned[id_field]))
            lineage = RecordLineage(source_file_id=source_file_id, source_file_name=source_name, source_locator=row["locator"], ingestion_job_id=job["job_id"])
            prepared.append((row, CanonicalRecord(entity.name, cleaned, extras, tuple(notes), tuple(issues), lineage)))
        pattern = identifier_pattern(id_values) if id_values else None
        # The OCR flags staged with the parsed row are carried into the row
        # rewritten below, so normalising it again validates the same way.
        row_ocr: list[bool] = []
        row_confidence: list[Any] = []
        for row, record in prepared:
            flags = row["normalized"] if isinstance(row.get("normalized"), dict) else {}
            ocr, confidence = bool(flags.get("_ocr", False)), flags.get("_ocr_confidence")
            row_ocr.append(ocr)
            row_confidence.append(confidence)
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
        self._heartbeat(institution_id, job["job_id"])
        # A row that cannot be imported claims no key and matches no one: the
        # first importable copy owns the key, and no question asks about a row
        # that would not be imported whatever the answer.
        importable = [index for index, record in enumerate(records) if not record.has_blocking_issues()]
        candidates, found, duplicate_warnings = find_duplicates([records[index] for index in importable], existing)
        actions = {importable[position]: action for position, action in found.items()}
        self._heartbeat(institution_id, job["job_id"])
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
                "normalized": {"fields": record.fields, "attributes": record.attributes, "normalizations": list(record.normalizations), "_ocr": row_ocr[index], "_ocr_confidence": row_confidence[index]},
                "status": status, "action": action, "record_key": record.record_key, "issues": list(record.issues),
            })
        self.store.replace_job_records(institution_id, job["job_id"], updated_rows)
        # Decisions count only for the questions of this run (see _locators_to_skip).
        run_id = uuid4().hex
        review_items = [{"kind": "duplicate", "payload": {**candidate.as_dict(), "normalization_run": run_id}} for candidate in candidates if candidate.kind in {"conflicting_key", "probable_person"}]
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
            "run_id": run_id,
        }
        if not any(item["status"] in {"ready", "review"} for item in updated_rows):
            # Finishing as an empty "imported" job would hide why nothing arrived.
            return self.store.update_job(institution_id, job["job_id"], status=JOB_FAILED, stage=STAGE_NORMALIZING, report=report, error=_nothing_importable(updated_rows))
        if review_items:
            self.store.add_review_items(institution_id, job["job_id"], review_items)
            return self.store.update_job(institution_id, job["job_id"], status=JOB_NEEDS_REVIEW, stage=STAGE_DUPLICATE_REVIEW, report=report)
        job = self.store.update_job(institution_id, job["job_id"], status=JOB_READY, stage=STAGE_READY, report=report)
        if before_commit is not None:
            before_commit()
        if (job.get("options") or {}).get("auto_commit"):
            return self.commit(institution_id, job["job_id"], committed_by=job["requested_by"])
        return job

    def resolve_review(self, institution_id: str, review_id: str, *, decision: str, resolved_by: str, note: str = "", defer_commit: bool = False) -> dict[str, Any]:
        """Resolve a pending duplicate review item; mapping items go through ``apply_mapping``.

        When the last decision lets an auto-commit job import, the import runs
        here, or with ``defer_commit`` is only requested (``request_commit``) so
        the caller can hand it to the job queue.
        """

        pending_item = self.store.get_review_item(institution_id, review_id)
        if pending_item is None or pending_item["status"] != "pending":
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
                if defer_commit:
                    return self.request_commit(institution_id, job["job_id"], committed_by=resolved_by)
                return self.commit(institution_id, job["job_id"], committed_by=resolved_by)
        return job

    # ------------------------------------------------------------------ commit
    def commit(self, institution_id: str, job_id: str, *, committed_by: str) -> dict[str, Any]:
        """Import the staged rows; the API runs ``request_commit`` in the request and ``run_commit`` on the job queue."""

        self.request_commit(institution_id, job_id, committed_by=committed_by)
        return self.run_commit(institution_id, job_id)

    def request_commit(self, institution_id: str, job_id: str, *, committed_by: str) -> dict[str, Any]:
        """Check that the job can be imported and record who asked; nothing is imported yet.

        The job is left ``processing`` at ``importing``, which is where
        ``run_commit`` (or a restart after an interruption) starts.
        """

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
        report = dict(job.get("report") or {})
        report["commit"] = {"committed_by": committed_by}
        return self.store.update_job(institution_id, job_id, status=JOB_PROCESSING, stage=STAGE_IMPORTING, report=report, error=None)

    def run_commit(self, institution_id: str, job_id: str) -> dict[str, Any]:
        """Import the rows of a job ``request_commit`` recorded; a failure returns the job to ready with the error."""

        job = self._job(institution_id, job_id)
        committed_by = str(((job.get("report") or {}).get("commit") or {}).get("committed_by") or job["requested_by"])
        try:
            entity = CANONICAL_ENTITIES[job["entity"]]
            skip_locators = self._locators_to_skip(institution_id, job, entity, self._review_items(institution_id, job_id))
            return self._import(institution_id, job, entity, skip_locators, committed_by=committed_by)
        except Exception as exc:  # noqa: BLE001 - the job must not stay in importing
            logger.exception("ingestion job %s for institution %s failed while importing", job_id, institution_id)
            message = f"the import failed ({type(exc).__name__}); the job can be committed again once the cause is fixed"
            self.store.update_job(institution_id, job_id, status=JOB_READY, stage=STAGE_READY, error=message)
            raise CommitError(message) from exc

    def _review_items(self, institution_id: str, job_id: str) -> list[dict[str, Any]]:
        """Every review item of a job, decided or not, read page by page."""

        items: list[dict[str, Any]] = []
        while True:
            page = self.store.list_review_items(institution_id, job_id=job_id, status=None, limit=REVIEW_PAGE_SIZE, offset=len(items))
            items.extend(page)
            if len(page) < REVIEW_PAGE_SIZE:
                return items

    def _locators_to_skip(self, institution_id: str, job: Mapping[str, Any], entity: CanonicalEntity, items: Iterable[Mapping[str, Any]]) -> set[str]:
        """Rows the reviewer's duplicate decisions keep out of the import.

        Conflicting copies of one key: approving keeps the earlier copy and
        skips the reviewed one; rejecting ("use this row") imports the
        reviewed copy instead of the others (the latest one when several are
        chosen). Approving "same person" must drop the identity that would be
        a second one, never the legitimate update of the record that already
        exists: for a match against an existing record that is the new row's
        key; for a pair inside the batch it is the key not yet known (or the
        later row's when both are new). Every copy of a dropped key is
        skipped, whatever was chosen among the copies. Two rows that both
        update existing records are both imported: they already are separate
        records and merging them is not an import decision.
        """

        # Only a person's decisions on this run's questions count: questions of an
        # earlier, superseded run were closed by the system, not answered.
        run_id = ((job.get("report") or {}).get("normalization") or {}).get("run_id")
        decided = [
            item for item in items
            if item["kind"] == "duplicate" and item["status"] in {"approved", "rejected"} and item.get("resolved_by") != "system"
            and (item.get("payload") or {}).get("normalization_run") == run_id
        ]
        if not decided:
            return set()
        rows = {row["locator"]: row for row in self.store.job_records(institution_id, job["job_id"], limit=self.max_rows)}
        keys = {str(row.get("record_key")) for row in rows.values() if row.get("record_key") and str(row["record_key"]).strip("|")}
        existing = self.store.existing_keys(institution_id, entity.name, sorted(keys)) if keys else {}
        skip: set[str] = set()
        dropped_keys: set[str] = set()
        chosen: dict[str, tuple[int, str]] = {}

        def drop(locator: str) -> None:
            key = str(rows.get(locator, {}).get("record_key") or "")
            if key.strip("|"):
                dropped_keys.add(key)
            else:
                skip.add(locator)

        for item in decided:
            payload = item.get("payload", {})
            left, right = str(payload.get("left_locator") or ""), payload.get("right_locator")
            if payload.get("kind") == "conflicting_key":
                row = rows.get(left, {})
                if item["status"] == "approved":
                    skip.add(left)
                elif row.get("status") in {"ready", "review"} and str(row.get("record_key") or "").strip("|"):
                    key, number = str(row["record_key"]), int(row.get("row_number") or 0)
                    if key not in chosen or number > chosen[key][0]:
                        chosen[key] = (number, left)
                continue
            if item["status"] != "approved":
                continue
            left_known = str(rows.get(left, {}).get("record_key") or "") in existing
            if not right:
                if not left_known:
                    drop(left)
                continue
            right = str(right)
            right_known = str(rows.get(right, {}).get("record_key") or "") in existing
            if left_known and not right_known:
                drop(right)
            elif right_known and not left_known:
                drop(left)
            elif not left_known and not right_known:
                drop(right)
        for key, (_, keep) in chosen.items():
            skip.update(locator for locator, row in rows.items() if row.get("record_key") == key and locator != keep)
        skip.update(locator for locator, row in rows.items() if row.get("record_key") in dropped_keys)
        return skip

    def _import(self, institution_id: str, job: dict[str, Any], entity: CanonicalEntity, skip_locators: set[str], *, committed_by: str) -> dict[str, Any]:
        job_id = job["job_id"]
        staged = self.store.job_records(institution_id, job_id, limit=self.max_rows)
        records: list[CanonicalRecord] = []
        reasons: Counter[str] = Counter()
        for row in staged:
            if row["status"] != "ready" and row["status"] != "review":
                reasons[_skip_reason(row)] += 1
                continue
            if row["locator"] in skip_locators:
                reasons["duplicate_review_decision"] += 1
                continue
            normalized = row.get("normalized") or {}
            lineage = RecordLineage(source_file_id=job.get("file_id"), source_file_name=str(job.get("report", {}).get("parse", {}).get("file_name") or (job.get("options") or {}).get("source_label") or ""), source_locator=row["locator"], ingestion_job_id=job_id)
            records.append(CanonicalRecord(entity.name, dict(normalized.get("fields", {})), dict(normalized.get("attributes", {})), tuple(normalized.get("normalizations", [])), tuple(row.get("issues", [])), lineage))
        skipped = sum(reasons.values())
        if not records:
            report = dict(job.get("report") or {})
            report["import"] = {"entity": entity.name, "inserted": 0, "updated": 0, "unchanged": 0, "skipped": skipped, "skipped_reasons": dict(reasons), "committed_by": committed_by, "message": "no rows were eligible for import"}
            return self.store.update_job(institution_id, job_id, status=JOB_IMPORTED, stage=STAGE_DONE, report=report)
        # The fields this import supplies: the targets of the mapping the rows
        # were normalised under (every normalised row carries each of them,
        # blank or not) and what cleaning derived from them, such as a name
        # from first and last name or a percent from counts. An update writes
        # only these, so a sheet with some of a record's columns keeps the rest.
        mapped_fields = {name for record in records for name in record.fields}
        summary = self.store.upsert_records(institution_id, records, mapped_fields=mapped_fields)
        summary.skipped += skipped
        for reason, count in reasons.items():
            summary.skipped_reasons[reason] = summary.skipped_reasons.get(reason, 0) + count
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
    "CommitError", "IngestionError", "IngestionService", "ProcessingError", "JOB_FAILED", "JOB_IMPORTED", "JOB_NEEDS_REVIEW", "JOB_PROCESSING", "JOB_QUEUED", "JOB_READY",
    "STAGE_DONE", "STAGE_DUPLICATE_REVIEW", "STAGE_IMPORTING", "STAGE_MAPPING", "STAGE_MAPPING_REVIEW", "STAGE_NORMALIZING", "STAGE_PARSING", "STAGE_READY",
]
