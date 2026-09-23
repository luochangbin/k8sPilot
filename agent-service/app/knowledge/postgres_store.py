"""PostgreSQL + pgvector storage for curated knowledge and verified incidents."""

from __future__ import annotations

import hashlib
import json
import math
import re
from datetime import datetime
from typing import Any, Optional

from .models import IncidentCase, KnowledgeChunk, KnowledgeDocument

_IDENT = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]{0,62}$")


class PostgresKnowledgeStore:
    """Small-corpus exact vector + lexical RRF store; no ANN index is created."""

    def __init__(self, database_url: str, embeddings: Any, *, schema: str = "public",
                 allowed_acl_tags: Optional[list[str]] = None) -> None:
        if not _IDENT.fullmatch(schema):
            raise ValueError("unsafe PostgreSQL schema identifier")
        if not database_url:
            raise ValueError("database_url is required")
        self.database_url = database_url
        self.embeddings = embeddings
        self.schema = schema
        self.allowed_acl_tags = frozenset(allowed_acl_tags or [])
        self.model_id = str(embeddings.model_id)
        self.dimensions = int(embeddings.dimensions)
        if self.dimensions <= 0 or not self.model_id:
            raise ValueError("embedding model_id and positive dimensions are required")
        self._initialize()

    def _connect(self):
        try:
            import psycopg
            from psycopg import sql
        except ImportError as exc:
            raise RuntimeError("psycopg is required for PostgreSQL knowledge storage") from exc
        conn = psycopg.connect(self.database_url, connect_timeout=5)
        conn.execute("SET statement_timeout = '10s'")
        conn.execute("SET lock_timeout = '5s'")
        conn.execute(sql.SQL("SET search_path TO {}, public").format(sql.Identifier(self.schema)))
        return conn

    def _initialize(self) -> None:
        from psycopg import sql
        with self._connect() as conn:
            conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
            self._ensure_schema(conn)
            conn.execute(sql.SQL("SET search_path TO {}, public").format(sql.Identifier(self.schema)))
            conn.execute("""CREATE TABLE IF NOT EXISTS knowledge_embedding_config (
                singleton boolean PRIMARY KEY DEFAULT true CHECK (singleton),
                model_id text NOT NULL, dimensions integer NOT NULL
            )""")
            row = conn.execute("SELECT model_id, dimensions FROM knowledge_embedding_config WHERE singleton=true").fetchone()
            if row and (row[0] != self.model_id or row[1] != self.dimensions):
                raise ValueError(f"embedding configuration mismatch: database={row[0]}/{row[1]}, configured={self.model_id}/{self.dimensions}")
            if not row:
                conn.execute("INSERT INTO knowledge_embedding_config(singleton,model_id,dimensions) VALUES(true,%s,%s)", (self.model_id, self.dimensions))
            conn.execute("""CREATE TABLE IF NOT EXISTS knowledge_documents (
                document_id text PRIMARY KEY, source_type text NOT NULL, title text NOT NULL,
                source_uri text NOT NULL DEFAULT '', product text NOT NULL DEFAULT '', versions jsonb NOT NULL DEFAULT '[]',
                environments jsonb NOT NULL DEFAULT '[]', owner text NOT NULL DEFAULT '', valid_from timestamptz,
                valid_until timestamptz, checksum text NOT NULL DEFAULT '', acl_tags jsonb NOT NULL DEFAULT '[]',
                status text NOT NULL, content text NOT NULL DEFAULT '', updated_at timestamptz,
                source_format text, resource_kinds jsonb NOT NULL DEFAULT '[]', model_id text NOT NULL,
                dimensions integer NOT NULL, metadata_hash text NOT NULL
            )""")
            conn.execute(sql.SQL("""CREATE TABLE IF NOT EXISTS knowledge_chunks (
                chunk_id text PRIMARY KEY, document_id text NOT NULL REFERENCES knowledge_documents(document_id) ON DELETE CASCADE,
                section text NOT NULL, content text NOT NULL, page_start integer, page_end integer,
                embedding vector({}) NOT NULL, model_id text NOT NULL, dimensions integer NOT NULL,
                search_text tsvector NOT NULL DEFAULT ''::tsvector
            )""").format(sql.Literal(self.dimensions)))
            conn.execute("CREATE INDEX IF NOT EXISTS knowledge_chunks_search_idx ON knowledge_chunks USING gin(search_text)")
            conn.execute("""CREATE TABLE IF NOT EXISTS knowledge_incidents (
                incident_id text PRIMARY KEY, status text NOT NULL, product text NOT NULL DEFAULT '',
                product_version text NOT NULL DEFAULT '', environment text NOT NULL DEFAULT '', resource_kind text NOT NULL DEFAULT '',
                symptoms jsonb NOT NULL DEFAULT '[]', evidence_signature jsonb NOT NULL DEFAULT '[]',
                root_cause_code text NOT NULL DEFAULT '', remediation_summary text NOT NULL DEFAULT '',
                verification jsonb NOT NULL DEFAULT '{}', evidence_summary text NOT NULL DEFAULT '', checksum text NOT NULL DEFAULT '',
                search_text tsvector NOT NULL DEFAULT ''::tsvector, metadata_hash text NOT NULL DEFAULT ''
            )""")
            conn.execute("CREATE INDEX IF NOT EXISTS knowledge_incidents_search_idx ON knowledge_incidents USING gin(search_text)")

    def _ensure_schema(self, conn) -> None:
        row = conn.execute("SELECT to_regnamespace(%s)", (self.schema,)).fetchone()
        if row and row[0] is not None:
            return
        from psycopg import sql
        try:
            conn.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(self.schema)))
        except Exception as exc:
            if getattr(exc, "sqlstate", None) == "42501":
                raise RuntimeError(
                    f"schema {self.schema!r} does not exist and the database role cannot create it; "
                    "ask a database administrator to provision the schema and grant USAGE, CREATE"
                ) from None
            raise

    def _ensure_embedding_config(self, conn) -> None:
        row = conn.execute("SELECT model_id, dimensions FROM knowledge_embedding_config WHERE singleton=true").fetchone()
        if not row or row[0] != self.model_id or row[1] != self.dimensions:
            raise ValueError("embedding model/dimensions no longer match this knowledge database")

    def upsert_document(self, doc: KnowledgeDocument, chunks: list[KnowledgeChunk]) -> bool:
        checksum = doc.checksum or hashlib.sha256(doc.content.encode("utf-8")).hexdigest()
        metadata = _document_metadata(doc)
        metadata_hash = hashlib.sha256(json.dumps(metadata, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        with self._connect() as conn:
            self._ensure_embedding_config(conn)
            old = conn.execute("SELECT checksum, model_id, dimensions, metadata_hash FROM knowledge_documents WHERE document_id=%s", (doc.document_id,)).fetchone()
        if old and old == (checksum, self.model_id, self.dimensions, metadata_hash):
            return False

        texts = [f"{chunk.section}\n{chunk.content}" for chunk in chunks]
        vectors = self.embeddings.embed_documents(texts) if texts else []
        if len(vectors) != len(texts):
            raise ValueError("embedding count does not match chunk count")
        checked_vectors = [_validate_vector(v, self.dimensions) for v in vectors]
        for chunk in chunks:
            if chunk.document_id != doc.document_id:
                raise ValueError("chunk document_id does not match document")

        with self._connect() as conn:
            self._ensure_embedding_config(conn)
            conn.execute("""INSERT INTO knowledge_documents
                (document_id,source_type,title,source_uri,product,versions,environments,owner,valid_from,valid_until,checksum,acl_tags,status,content,updated_at,source_format,resource_kinds,model_id,dimensions,metadata_hash)
                VALUES(%s,%s,%s,%s,%s,%s::jsonb,%s::jsonb,%s,%s,%s,%s,%s::jsonb,%s,%s,%s,%s,%s::jsonb,%s,%s,%s)
                ON CONFLICT(document_id) DO UPDATE SET source_type=excluded.source_type,title=excluded.title,
                source_uri=excluded.source_uri,product=excluded.product,versions=excluded.versions,environments=excluded.environments,
                owner=excluded.owner,valid_from=excluded.valid_from,valid_until=excluded.valid_until,checksum=excluded.checksum,
                acl_tags=excluded.acl_tags,status=excluded.status,content=excluded.content,updated_at=excluded.updated_at,
                source_format=excluded.source_format,resource_kinds=excluded.resource_kinds,model_id=excluded.model_id,
                dimensions=excluded.dimensions,metadata_hash=excluded.metadata_hash""",
                (doc.document_id,doc.source_type,doc.title,doc.source_uri,doc.product,_json(doc.versions),_json(doc.environments),doc.owner,
                 _dt(doc.valid_from),_dt(doc.valid_until),checksum,_json(doc.acl_tags),doc.status,doc.content,_dt(doc.updated_at),doc.source_format,
                 _json(doc.resource_kinds),self.model_id,self.dimensions,metadata_hash))
            conn.execute("DELETE FROM knowledge_chunks WHERE document_id=%s", (doc.document_id,))
            for chunk, vector in zip(chunks, checked_vectors):
                search_text = _lex_index_text(f"{chunk.section} {chunk.content}")
                conn.execute("""INSERT INTO knowledge_chunks(chunk_id,document_id,section,content,page_start,page_end,embedding,model_id,dimensions,search_text)
                    VALUES(%s,%s,%s,%s,%s,%s,%s::vector,%s,%s,to_tsvector('simple',%s))""",
                    (chunk.chunk_id,doc.document_id,chunk.section,chunk.content,chunk.page_start,chunk.page_end,
                     _vector_text(vector),self.model_id,self.dimensions,search_text))
        return True

    def upsert_incident(self, incident: IncidentCase) -> bool:
        checksum = incident.checksum or hashlib.sha256(json.dumps(_incident_metadata(incident),sort_keys=True,ensure_ascii=False).encode()).hexdigest()
        metadata_hash=hashlib.sha256(json.dumps(_incident_metadata(incident),sort_keys=True,ensure_ascii=False).encode()).hexdigest()
        with self._connect() as conn:
            row = conn.execute("SELECT checksum,status,metadata_hash FROM knowledge_incidents WHERE incident_id=%s", (incident.incident_id,)).fetchone()
            if row and row == (checksum, incident.status, metadata_hash):
                return False
            search = _lex_index_text(" ".join([*incident.symptoms, incident.root_cause_code, incident.evidence_summary, incident.remediation_summary]))
            conn.execute("""INSERT INTO knowledge_incidents
                (incident_id,status,product,product_version,environment,resource_kind,symptoms,evidence_signature,root_cause_code,remediation_summary,verification,evidence_summary,checksum,search_text,metadata_hash)
                VALUES(%s,%s,%s,%s,%s,%s,%s::jsonb,%s::jsonb,%s,%s,%s::jsonb,%s,%s,to_tsvector('simple',%s),%s)
                ON CONFLICT(incident_id) DO UPDATE SET status=excluded.status,product=excluded.product,product_version=excluded.product_version,
                environment=excluded.environment,resource_kind=excluded.resource_kind,symptoms=excluded.symptoms,
                evidence_signature=excluded.evidence_signature,root_cause_code=excluded.root_cause_code,
                remediation_summary=excluded.remediation_summary,verification=excluded.verification,
                evidence_summary=excluded.evidence_summary,checksum=excluded.checksum,search_text=excluded.search_text,metadata_hash=excluded.metadata_hash""",
                (incident.incident_id,incident.status,incident.product,incident.product_version,incident.environment,incident.resource_kind,
                 _json(incident.symptoms),_json(incident.evidence_signature),incident.root_cause_code,incident.remediation_summary,
                 _json(incident.verification),incident.evidence_summary,checksum,search,metadata_hash))
        return True

    def list_documents(self, status: Optional[str] = None) -> list[KnowledgeDocument]:
        with self._connect() as conn:
            if status:
                rows = conn.execute("SELECT * FROM knowledge_documents WHERE status=%s ORDER BY document_id", (status,)).fetchall()
            else:
                rows = conn.execute("SELECT * FROM knowledge_documents ORDER BY document_id").fetchall()
        return [_document_from_row(row) for row in rows]

    def list_incidents(self, status: Optional[str] = None) -> list[IncidentCase]:
        with self._connect() as conn:
            if status:
                rows = conn.execute("SELECT * FROM knowledge_incidents WHERE status=%s ORDER BY incident_id", (status,)).fetchall()
            else:
                rows = conn.execute("SELECT * FROM knowledge_incidents ORDER BY incident_id").fetchall()
        return [_incident_from_row(row) for row in rows]

    def search_chunks(self, query: str, top_k: int, filters: Optional[dict[str, Any]] = None) -> list[dict[str, Any]]:
        qvec = _vector_text(_validate_vector(self.embeddings.embed_query(query), self.dimensions))
        terms = _lex_terms(query)
        tsquery = " | ".join(terms)
        filters = filters or {}
        where_sql, base_values = _document_filter_parts(filters, self.allowed_acl_tags)
        query_sql = f"""WITH lex AS (
            SELECT c.chunk_id, row_number() OVER(ORDER BY ts_rank_cd(c.search_text,to_tsquery('simple',%s)) DESC,c.chunk_id) lr
            FROM knowledge_chunks c JOIN knowledge_documents d USING(document_id)
            WHERE {where_sql} AND c.search_text @@ to_tsquery('simple',%s)
            ORDER BY ts_rank_cd(c.search_text,to_tsquery('simple',%s)) DESC,c.chunk_id LIMIT %s
        ), vec AS (
            SELECT c.chunk_id,row_number() OVER(ORDER BY c.embedding <=> %s::vector,c.chunk_id) vr
            FROM knowledge_chunks c JOIN knowledge_documents d USING(document_id) WHERE {where_sql} ORDER BY c.embedding <=> %s::vector,c.chunk_id LIMIT %s
        ), fused AS (
            SELECT chunk_id, sum(1.0/(60+rank_no)) rrf FROM (
                SELECT chunk_id,lr rank_no FROM lex UNION ALL SELECT chunk_id,vr FROM vec
            ) x GROUP BY chunk_id ORDER BY rrf DESC,chunk_id LIMIT %s
        )
        SELECT c.chunk_id,c.section,c.content,c.page_start,c.page_end,f.rrf,
          d.document_id,d.source_type,d.title,d.source_uri,d.product,d.versions,d.environments,d.owner,
          d.valid_from,d.valid_until,d.checksum,d.acl_tags,d.status,d.content,d.updated_at,d.source_format,d.resource_kinds
        FROM fused f JOIN knowledge_chunks c USING(chunk_id) JOIN knowledge_documents d USING(document_id)
        ORDER BY f.rrf DESC,c.chunk_id"""
        bound = [tsquery, *base_values, tsquery, tsquery, max(1,min(int(top_k),50)), qvec, *base_values, qvec,
                 max(1,min(int(top_k),50)), max(1,min(int(top_k),50))]
        with self._connect() as conn:
            rows = conn.execute(query_sql, bound).fetchall()
        return [_chunk_hit(row) for row in rows]

    def search_incidents(self, query: str, top_k: int, filters: Optional[dict[str, Any]] = None) -> list[dict[str, Any]]:
        terms = _lex_terms(query)
        tsquery = " | ".join(terms)
        filters = filters or {}
        where = ["status='verified'", "search_text @@ to_tsquery('simple',%s)"]
        params: list[Any] = [tsquery]
        for key, expr in (("product","product=%s"),("product_version","product_version=%s"),("environment","environment=%s"),("resource_kind","resource_kind=%s")):
            if filters.get(key): where.append(expr); params.append(filters[key])
        if filters.get("root_cause_candidates"):
            where.append("root_cause_code = ANY(%s)"); params.append(list(filters["root_cause_candidates"]))
        params.append(max(1,min(int(top_k),50)))
        with self._connect() as conn:
            rows = conn.execute(f"SELECT incident_id,status,product,product_version,environment,resource_kind,symptoms,evidence_signature,root_cause_code,remediation_summary,verification,evidence_summary,checksum,-ts_rank_cd(search_text,to_tsquery('simple',%s)) rank FROM knowledge_incidents WHERE {' AND '.join(where)} ORDER BY ts_rank_cd(search_text,to_tsquery('simple',%s)) DESC,incident_id LIMIT %s",
                                [tsquery,*params[:-1],tsquery,params[-1]]).fetchall()
        keys = ("incident_id","status","product","product_version","environment","resource_kind","symptoms","evidence_signature","root_cause_code","remediation_summary","verification","evidence_summary","checksum","rank")
        return [dict(zip(keys,row)) for row in rows]

    def document_checksum(self, document_id: str) -> Optional[str]:
        with self._connect() as conn:
            row=conn.execute("SELECT checksum FROM knowledge_documents WHERE document_id=%s",(document_id,)).fetchone()
        return row[0] if row else None

    def delete_document(self, document_id: str) -> bool:
        with self._connect() as conn:
            return conn.execute("DELETE FROM knowledge_documents WHERE document_id=%s",(document_id,)).rowcount > 0

    def delete_incident(self, incident_id: str) -> bool:
        with self._connect() as conn:
            return conn.execute("DELETE FROM knowledge_incidents WHERE incident_id=%s",(incident_id,)).rowcount > 0

    def snapshot_hash(self) -> str:
        with self._connect() as conn:
            docs=conn.execute("SELECT document_id,checksum,metadata_hash FROM knowledge_documents ORDER BY document_id").fetchall()
            incidents=conn.execute("SELECT incident_id,checksum,status FROM knowledge_incidents ORDER BY incident_id").fetchall()
        return hashlib.sha256(json.dumps([self.model_id,self.dimensions,docs,incidents],sort_keys=True,default=str).encode()).hexdigest()


def _lex_terms(query: str) -> list[str]:
    # PostgreSQL's simple dictionary does not segment Chinese; generate explicit CJK uni/bi-grams.
    ascii_terms = re.findall(r"[A-Za-z0-9_]+", query.lower())
    cjk = "".join(re.findall(r"[\u3400-\u9fff]", query))
    cjk_terms = list(cjk) + [cjk[i:i+2] for i in range(len(cjk)-1)]
    return list(dict.fromkeys(t for t in [*ascii_terms,*cjk_terms] if len(t) >= 1))[:64] or ["__no_match__"]


def _document_filter_parts(filters: dict[str, Any], allowed_acl_tags: set[str] | frozenset[str]) -> tuple[str, list[Any]]:
    clauses = ["d.status='active'", "(d.valid_from IS NULL OR d.valid_from<=now())",
               "(d.valid_until IS NULL OR d.valid_until>now())",
               "(jsonb_array_length(d.acl_tags)=0 OR d.acl_tags ?| %s)"]
    values: list[Any] = [sorted(allowed_acl_tags)]
    for key, clause in (("product", "d.product=%s"), ("environment", "d.environments ? %s"),
                        ("resource_kind", "d.resource_kinds ? %s")):
        if filters.get(key):
            clauses.append(clause)
            values.append(filters[key])
    for key, clause in (("versions", "d.versions ?| %s"), ("source_types", "d.source_type = ANY(%s)")):
        if filters.get(key):
            clauses.append(clause)
            values.append(list(filters[key]))
    return " AND ".join(clauses), values


def _lex_index_text(text: str) -> str:
    ascii_terms = re.findall(r"[A-Za-z0-9_]+", text.lower())
    cjk = "".join(re.findall(r"[\u3400-\u9fff]", text))
    cjk_terms = list(cjk) + [cjk[i:i+2] for i in range(len(cjk)-1)]
    return " ".join(dict.fromkeys([*ascii_terms,*cjk_terms]))


def _validate_vector(vector: Any, dimensions: int) -> list[float]:
    result=[float(x) for x in vector]
    if len(result) != dimensions or not all(math.isfinite(x) for x in result):
        raise ValueError(f"embedding must contain {dimensions} finite values")
    return result


def _vector_text(vector: list[float]) -> str:
    return "[" + ",".join(format(x, ".9g") for x in vector) + "]"


def _json(value: Any) -> str:
    return json.dumps(value,ensure_ascii=False,separators=(",",":"))


def _dt(value: Optional[str]):
    if not value: return None
    return datetime.fromisoformat(value.replace("Z","+00:00"))


def _document_metadata(doc: KnowledgeDocument) -> dict[str, Any]:
    return {"source_type":doc.source_type,"title":doc.title,"source_uri":doc.source_uri,"product":doc.product,
            "versions":doc.versions,"environments":doc.environments,"owner":doc.owner,"valid_from":doc.valid_from,
            "valid_until":doc.valid_until,"acl_tags":doc.acl_tags,"status":doc.status,"updated_at":doc.updated_at,
            "source_format":doc.source_format,"resource_kinds":doc.resource_kinds}


def _incident_metadata(case: IncidentCase) -> dict[str, Any]:
    return {"status":case.status,"product":case.product,"product_version":case.product_version,"environment":case.environment,
            "resource_kind":case.resource_kind,"symptoms":case.symptoms,"evidence_signature":case.evidence_signature,
            "root_cause_code":case.root_cause_code,"remediation_summary":case.remediation_summary,
            "verification":case.verification,"evidence_summary":case.evidence_summary}


def _decode(value: Any) -> Any:
    return value if not isinstance(value,str) else json.loads(value)


def _document_from_row(row: tuple[Any, ...]) -> KnowledgeDocument:
    return KnowledgeDocument(document_id=row[0],source_type=row[1],title=row[2],source_uri=row[3],product=row[4],
        versions=_decode(row[5]),environments=_decode(row[6]),owner=row[7],valid_from=_date_text(row[8]),
        valid_until=_date_text(row[9]),checksum=row[10],acl_tags=_decode(row[11]),status=row[12],content=row[13],
        updated_at=_date_text(row[14]),source_format=row[15],resource_kinds=_decode(row[16]))


def _incident_from_row(row: tuple[Any, ...]) -> IncidentCase:
    return IncidentCase(incident_id=row[0],status=row[1],product=row[2],product_version=row[3],environment=row[4],
        resource_kind=row[5],symptoms=_decode(row[6]),evidence_signature=_decode(row[7]),root_cause_code=row[8],
        remediation_summary=row[9],verification=_decode(row[10]),evidence_summary=row[11],checksum=row[12])


def _date_text(value: Any) -> Optional[str]:
    return value.isoformat() if value is not None else None


def _chunk_hit(row: tuple[Any, ...]) -> dict[str, Any]:
    doc=KnowledgeDocument(document_id=row[6],source_type=row[7],title=row[8],source_uri=row[9],product=row[10],
        versions=_decode(row[11]),environments=_decode(row[12]),owner=row[13],valid_from=row[14].isoformat() if row[14] else None,
        valid_until=row[15].isoformat() if row[15] else None,checksum=row[16],acl_tags=_decode(row[17]),status=row[18],content=row[19],
        updated_at=row[20].isoformat() if row[20] else None,source_format=row[21],resource_kinds=_decode(row[22]))
    return {"chunk_id":row[0],"section":row[1],"chunk_content":row[2],"page_start":row[3],"page_end":row[4],"rank":-float(row[5]),"document":doc}
