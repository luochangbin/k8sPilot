"""Safe, incremental Markdown/PDF knowledge import."""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from .models import IncidentCase, KnowledgeChunk, KnowledgeDocument

PARSER_VERSION = "knowledge-import-v1"
MAX_FILE_BYTES = 20 * 1024 * 1024
MAX_PDF_PAGES = 500
MAX_METADATA_BYTES = 16 * 1024
MAX_CHUNK_CHARS = 4000
_DOC_TYPES = {"runbook", "product_doc", "known_issue", "sop"}
_DOC_FIELDS = {
    "document_id", "source_type", "title", "source_uri", "product", "versions",
    "environments", "owner", "valid_from", "valid_until", "acl_tags", "status",
    "resource_kinds", "updated_at",
}
_INCIDENT_FIELDS = {
    "record_type", "kind", "incident_id", "status", "product", "product_version",
    "environment", "resource_kind", "symptoms", "evidence_signature",
    "root_cause_code", "remediation_summary", "verification", "evidence_summary",
}


@dataclass
class ImportResult:
    document_id: str
    status: str
    message: str


class ImportError(ValueError):
    """Input document cannot safely be imported."""


def import_path(path: str | Path, store: Any) -> list[ImportResult]:
    """Import one file or recursively import supported files from a directory.

    Directory scans never follow symlinks and never delete entries omitted from
    the current tree. Each source is parsed and validated before the store is
    changed, preserving the previous indexed version on failures.
    """
    root = Path(path)
    if root.is_symlink():
        raise ImportError(f"symlink input is not allowed: {root}")
    if not root.exists():
        raise ImportError(f"path does not exist: {root}")
    if root.is_file():
        files = [root]
    elif root.is_dir():
        files = _scan(root)
    else:
        raise ImportError(f"unsupported input path: {root}")

    results: list[ImportResult] = []
    errors: list[str] = []
    for file_path in files:
        try:
            results.append(_import_file(file_path, store))
        except ImportError as exc:
            errors.append(f"{file_path.name}: {exc}")
        except Exception as exc:
            errors.append(f"{file_path.name}: import failed ({type(exc).__name__})")
    if errors:
        results.extend(ImportResult("", "failed", message) for message in errors)
    return results


def _scan(root: Path) -> list[Path]:
    files: list[Path] = []
    for base, dirs, names in os.walk(root, followlinks=False):
        base_path = Path(base)
        symlink_dirs = [name for name in dirs if (base_path / name).is_symlink()]
        if symlink_dirs:
            raise ImportError(f"symlink directory is not allowed: {base_path / symlink_dirs[0]}")
        dirs.sort()
        for name in sorted(names):
            candidate = base_path / name
            if candidate.is_symlink():
                raise ImportError(f"symlink file is not allowed: {candidate}")
            if candidate.suffix.lower() in {".md", ".pdf"}:
                files.append(candidate)
    return files


def _import_file(path: Path, store: Any) -> ImportResult:
    size = path.stat().st_size
    if size > MAX_FILE_BYTES:
        raise ImportError(f"file exceeds {MAX_FILE_BYTES} byte limit")
    raw = path.read_bytes()
    metadata: dict[str, Any]
    page_ranges: list[tuple[int, str]] = []
    if path.suffix.lower() == ".md":
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise ImportError("Markdown must be UTF-8 encoded") from exc
        metadata, body = _frontmatter(text)
        content = body.strip()
        if not content:
            raise ImportError("Markdown body is empty")
    elif path.suffix.lower() == ".pdf":
        sidecar = path.with_name(path.stem + ".metadata.yaml")
        if not sidecar.exists():
            raise ImportError(f"PDF requires sidecar metadata: {sidecar.name}")
        if sidecar.is_symlink():
            raise ImportError("metadata sidecar symlink is not allowed")
        sidecar_raw = sidecar.read_bytes()
        if len(sidecar_raw) > MAX_METADATA_BYTES:
            raise ImportError("metadata exceeds size limit")
        metadata = _safe_yaml(sidecar_raw.decode("utf-8-sig"))
        content, page_ranges, warnings = _pdf_text(raw)
    else:
        raise ImportError("only .md and .pdf are supported")

    record_type = metadata.get("record_type", metadata.get("kind", "document"))
    if record_type == "incident":
        _validate_keys(metadata, _INCIDENT_FIELDS)
        incident = _incident(path, metadata, content)
        _validate_incident(incident)
        _set_checksum(incident, raw, metadata)
        if _sqlite_existing_checksum(store, incident.incident_id, incident=True) == incident.checksum:
            return ImportResult(incident.incident_id, "skipped", "content unchanged")
        changed = store.upsert_incident(incident)
        return ImportResult(incident.incident_id, "imported" if changed is not False else "skipped",
                            "incident indexed" if changed is not False else "content unchanged")

    _validate_keys(metadata, _DOC_FIELDS)
    source_type = metadata.get("source_type", "")
    if not isinstance(source_type, str) or source_type not in _DOC_TYPES:
        raise ImportError(f"source_type must be one of {', '.join(sorted(_DOC_TYPES))}")
    document_id = _stable_id(path, metadata.get("document_id"))
    title = _bounded_string(metadata.get("title") or path.stem, "title", 500)
    allowed = {key: value for key, value in metadata.items()
               if key in _DOC_FIELDS and key not in {"document_id", "status"}}
    status = metadata.get("status", "active")
    if not isinstance(status, str) or status not in {"active", "draft", "deprecated"}:
        raise ImportError("knowledge document status must be active, draft, or deprecated")
    for key in ("product", "owner", "valid_from", "valid_until", "updated_at"):
        if key in allowed and allowed[key] is not None:
            allowed[key] = _bounded_string(allowed[key], key, 300)
    for key in ("versions", "environments", "acl_tags", "resource_kinds"):
        allowed[key] = _str_list(metadata, key)
    allowed.update({
        "document_id": document_id,
        "source_type": source_type,
        "title": title,
        "source_uri": _source_uri(metadata.get("source_uri"), path.name),
        "status": status,
        "content": content,
        "checksum": _checksum(raw, metadata),
        "source_format": path.suffix.lower().lstrip("."),
    })
    doc = KnowledgeDocument(**allowed)
    chunks = _make_chunks(doc, page_ranges, store)
    if _sqlite_existing_checksum(store, doc.document_id) == doc.checksum:
        message = "content unchanged"
        if path.suffix.lower() == ".pdf" and warnings:
            message += "; " + "; ".join(warnings)
        return ImportResult(document_id, "skipped", message)
    changed = store.upsert_document(doc, chunks)
    message = "indexed"
    if path.suffix.lower() == ".pdf" and warnings:
        message += "; " + "; ".join(warnings)
    return ImportResult(document_id, "imported" if changed is not False else "skipped", message)


def _frontmatter(text: str) -> tuple[dict[str, Any], str]:
    if not text.startswith("---\n") and not text.startswith("---\r\n"):
        raise ImportError("Markdown requires YAML frontmatter")
    lines = text.splitlines(keepends=True)
    try:
        end = next(i for i in range(1, len(lines)) if lines[i].strip() == "---")
    except StopIteration as exc:
        raise ImportError("unterminated YAML frontmatter") from exc
    header = "".join(lines[1:end])
    if len(header.encode("utf-8")) > MAX_METADATA_BYTES:
        raise ImportError("frontmatter exceeds size limit")
    return _safe_yaml(header), "".join(lines[end + 1:])


def _safe_yaml(value: str) -> dict[str, Any]:
    try:
        parsed = yaml.safe_load(value) or {}
    except yaml.YAMLError as exc:
        raise ImportError("invalid YAML metadata") from exc
    if not isinstance(parsed, dict):
        raise ImportError("metadata must be a YAML mapping")
    return _normalize_yaml(parsed)


def _normalize_yaml(value: Any) -> Any:
    # PyYAML parses plain ISO dates into date/datetime objects; normalize them
    # before validation and checksum serialization to keep metadata portable.
    from datetime import date, datetime
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(key): _normalize_yaml(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_normalize_yaml(item) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise ImportError("metadata contains an unsupported YAML value")


def _validate_keys(metadata: dict[str, Any], allowed: set[str]) -> None:
    unknown = sorted(set(metadata) - allowed)
    if unknown:
        raise ImportError(f"unsupported metadata field(s): {', '.join(unknown)}")


def _bounded_string(value: Any, field: str, limit: int) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise ImportError(f"{field} must be a non-empty string of at most {limit} chars")
    return value.strip()


def _str_list(meta: dict[str, Any], key: str, limit: int = 32) -> list[str]:
    value = meta.get(key, [])
    if not isinstance(value, list) or len(value) > limit:
        raise ImportError(f"{key} must be a list with at most {limit} items")
    return [_bounded_string(item, key, 300) for item in value]


def _stable_id(path: Path, explicit: Any) -> str:
    if explicit is not None:
        return _bounded_string(explicit, "document_id", 200)
    source = path.resolve().as_posix().casefold()
    return "file-" + hashlib.sha256(source.encode("utf-8")).hexdigest()[:24]


def _incident(path: Path, meta: dict[str, Any], content: str) -> IncidentCase:
    incident_id = _bounded_string(
        meta.get("incident_id") or _stable_id(path, None), "incident_id", 200)
    status = meta.get("status", "draft")
    if status not in {"draft", "verified"}:
        raise ImportError("incident status must be draft or verified")
    symptoms = _str_list(meta, "symptoms")
    evidence = meta.get("evidence_signature", [])
    verification = meta.get("verification", {})
    if not isinstance(evidence, list) or len(evidence) > 100:
        raise ImportError("evidence_signature must be a bounded list")
    if not isinstance(verification, dict) or len(verification) > 64:
        raise ImportError("verification must be a bounded mapping")
    return IncidentCase(
        incident_id=incident_id,
        status=status,
        product=_bounded_optional(meta.get("product"), "product", 300),
        product_version=_bounded_optional(meta.get("product_version"), "product_version", 100),
        environment=_bounded_optional(meta.get("environment"), "environment", 100),
        resource_kind=_bounded_optional(meta.get("resource_kind"), "resource_kind", 100),
        symptoms=symptoms,
        evidence_signature=evidence,
        root_cause_code=_bounded_optional(meta.get("root_cause_code"), "root_cause_code", 200),
        remediation_summary=_bounded_optional(meta.get("remediation_summary"), "remediation_summary", 4000),
        verification=verification,
        evidence_summary=_bounded_optional(meta.get("evidence_summary"), "evidence_summary", 4000)
            or content[:4000],
    )


def _validate_incident(incident: IncidentCase) -> None:
    if incident.status == "verified":
        if not incident.root_cause_code:
            raise ImportError("verified incident requires root_cause_code")
        if not incident.verification:
            raise ImportError("verified incident requires verification details")


def _bounded_optional(value: Any, field: str, limit: int) -> str:
    if value is None or value == "":
        return ""
    return _bounded_string(value, field, limit)


def _source_uri(value: Any, filename: str) -> str:
    if value is None or value == "":
        return filename
    uri = _bounded_string(value, "source_uri", 2048)
    if not uri.startswith(("https://", "http://")):
        raise ImportError("source_uri must use http:// or https://")
    return uri


def _set_checksum(incident: IncidentCase, raw: bytes, metadata: dict[str, Any]) -> None:
    incident.checksum = _checksum(raw, metadata)


def _sqlite_existing_checksum(store: Any, record_id: str, incident: bool = False) -> str | None:
    # PostgreSQL compares checksum plus embedding identity atomically inside
    # upsert; only the legacy store needs this importer-level skip check.
    if getattr(store, "embeddings", None) is not None or not hasattr(store, "list_documents"):
        return None
    records = store.list_incidents() if incident else store.list_documents()
    key = "incident_id" if incident else "document_id"
    match = next((record for record in records if getattr(record, key) == record_id), None)
    return getattr(match, "checksum", None) if match else None


def _checksum(raw: bytes, metadata: dict[str, Any]) -> str:
    canonical_metadata = json.dumps(metadata, sort_keys=True, ensure_ascii=False,
                                    separators=(",", ":")).encode("utf-8")
    digest = hashlib.sha256()
    digest.update(PARSER_VERSION.encode("ascii"))
    digest.update(b"\0")
    digest.update(raw)
    digest.update(b"\0")
    digest.update(canonical_metadata)
    return digest.hexdigest()


def _pdf_text(raw: bytes) -> tuple[str, list[tuple[int, str]], list[str]]:
    from io import BytesIO
    from pypdf import PdfReader

    try:
        reader = PdfReader(BytesIO(raw), strict=True)
        if len(reader.pages) > MAX_PDF_PAGES:
            raise ImportError(f"PDF exceeds {MAX_PDF_PAGES} page limit")
        pages: list[tuple[int, str]] = []
        warnings: list[str] = []
        for index, page in enumerate(reader.pages, start=1):
            text = (page.extract_text() or "").strip()
            if text:
                pages.append((index, text))
                continue
            resources = page.get("/Resources")
            xobjects = resources.get("/XObject") if resources else None
            if xobjects and len(xobjects):
                raise ImportError(f"page {index} has image content but no extractable text; OCR is unsupported")
            if _has_drawing_content(page):
                raise ImportError(f"page {index} has non-text drawing content; OCR is unsupported")
            warnings.append(f"blank page {index} skipped")
        if not pages:
            raise ImportError("PDF has no extractable text")
    except ImportError:
        raise
    except Exception as exc:
        raise ImportError(f"cannot parse PDF ({type(exc).__name__})") from exc

    pieces = [f"## Page {page_num}\n{text}" for page_num, text in pages]
    return "\n\n".join(pieces), pages, warnings


def _make_chunks(doc: KnowledgeDocument, page_ranges: list[tuple[int, str]],
                 store: Any = None) -> list[KnowledgeChunk]:
    from .ingest import chunk_document

    tokenizer = getattr(getattr(store, "embeddings", None), "count_tokens", None)
    target_tokens = getattr(getattr(store, "embeddings", None),
                            "max_document_tokens", None)
    if not tokenizer:
        target_tokens = None
    chunks: list[KnowledgeChunk] = []
    if page_ranges:
        for page_num, page_text in page_ranges:
            page_doc = type("PageDoc", (), {"document_id": doc.document_id,
                                              "content": page_text})()
            page_chunks = chunk_document(page_doc, max_chars=MAX_CHUNK_CHARS)
            for chunk in page_chunks:
                chunk.section = f"Page {page_num}" + (f" / {chunk.section}" if chunk.section else "")
                if hasattr(chunk, "page_start"):
                    chunk.page_start = page_num
                    chunk.page_end = page_num
                chunks.append(chunk)
    else:
        chunks = chunk_document(doc, max_chars=MAX_CHUNK_CHARS)

    if not target_tokens:
        bounded = chunks
    else:
        bounded = []
        for chunk in chunks:
            bounded.extend(_fit_tokens(chunk, tokenizer, target_tokens))
    for index, chunk in enumerate(bounded, 1):
        chunk.chunk_id = f"{doc.document_id}-{index:03d}"
    return bounded


def _fit_tokens(chunk: KnowledgeChunk, count_tokens: Any, target: int) -> list[KnowledgeChunk]:
    if count_tokens([f"{chunk.section}\n{chunk.content}"])[0] <= target:
        return [chunk]
    remaining = chunk.content
    output: list[KnowledgeChunk] = []
    while remaining:
        low, high = 1, len(remaining)
        best = 0
        while low <= high:
            middle = (low + high) // 2
            if count_tokens([f"{chunk.section}\n{remaining[:middle]}"])[0] <= target:
                best = middle
                low = middle + 1
            else:
                high = middle - 1
        if best == 0:
            raise ImportError("embedding token limit is smaller than one text character")
        boundary = best
        if best < len(remaining):
            floor = best // 2
            newline = remaining.rfind("\n", floor, best)
            space = remaining.rfind(" ", floor, best)
            split = max(newline, space)
            if split > floor:
                boundary = split + 1
        content = remaining[:boundary]
        output.append(KnowledgeChunk(
            chunk_id=chunk.chunk_id, document_id=chunk.document_id,
            section=chunk.section, content=content, page_start=chunk.page_start,
            page_end=chunk.page_end,
        ))
        remaining = remaining[boundary:]
    return output


def _has_drawing_content(page: Any) -> bool:
    """Treat pages with visible non-text PDF operators as OCR-required pages."""
    try:
        from pypdf.generic import ContentStream
        contents = page.get_contents()
        if contents is None:
            return False
        stream = ContentStream(contents, page.pdf)
        visual_ops = {
            b"Do", b"sh", b"m", b"l", b"c", b"v", b"y", b"re", b"S", b"s",
            b"f", b"F", b"f*", b"B", b"B*", b"b", b"b*", b"BI",
            b"Tj", b"TJ", b"T*", b"'", b'"',
        }
        return any(operator in visual_ops for _, operator in stream.operations)
    except Exception:
        # Parsing ambiguity should fail closed instead of silently losing a page.
        return bool(page.get("/Contents"))
