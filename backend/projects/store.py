"""Project persistence — pluggable backends.

Three backends behind one interface:
  * ``LocalFileStore``  — JSON files in a local directory. Default; used in dev
    and unit tests (no Databricks needed).
  * ``VolumeStore``     — JSON files in a Unity Catalog volume via the Databricks
    Files API. Durable across App redeploys; needs a writable UC volume.
  * ``PostgresStore``   — one JSONB row per project in a Lakebase (Postgres)
    table. Durable across App redeploys with no UC volume required — the app
    already talks to Lakebase. Used when deployed here.

Selected by ``LBX_PROJECTS_BACKEND`` (local|volume|postgres):
  * local/volume  -> ``LBX_PROJECTS_DIR``
  * postgres      -> ``LBX_PROJECTS_PG_HOST`` / ``_DATABASE`` / ``_USER`` / ``_PORT``
    / ``_TABLE``, with the password from ``LBX_PROJECTS_PG_PASSWORD`` or the
    Databricks secret scope (``LBX_PROJECTS_PG_SECRET_SCOPE`` / ``_SECRET_KEY``).
"""
from __future__ import annotations

import functools
import json
import os
import uuid
from abc import ABC, abstractmethod
from pathlib import Path

from backend.projects.models import Project, ProjectSummary, to_summary


class ProjectStore(ABC):
    @abstractmethod
    def list(self) -> list[ProjectSummary]: ...
    @abstractmethod
    def get(self, project_id: str) -> Project | None: ...
    @abstractmethod
    def save(self, project: Project) -> None: ...
    @abstractmethod
    def delete(self, project_id: str) -> None: ...


class LocalFileStore(ProjectStore):
    def __init__(self, directory: str):
        self._dir = Path(directory).expanduser()
        self._dir.mkdir(parents=True, exist_ok=True)

    def _path(self, pid: str) -> Path:
        return self._dir / f"{pid}.json"

    def list(self) -> list[ProjectSummary]:
        out: list[ProjectSummary] = []
        for f in self._dir.glob("*.json"):
            try:
                out.append(to_summary(Project.model_validate_json(f.read_text())))
            except Exception:
                continue  # ignore corrupt/foreign files
        return sorted(out, key=lambda s: s.updated_at, reverse=True)

    def get(self, project_id: str) -> Project | None:
        p = self._path(project_id)
        return Project.model_validate_json(p.read_text()) if p.is_file() else None

    def save(self, project: Project) -> None:
        self._path(project.id).write_text(project.model_dump_json(indent=2))

    def delete(self, project_id: str) -> None:
        self._path(project_id).unlink(missing_ok=True)


class VolumeStore(ProjectStore):
    """Stores project JSON in a UC volume via the Databricks Files API.

    ``base_dir`` is a volume path like ``/Volumes/<catalog>/<schema>/<volume>/projects``.
    """

    def __init__(self, base_dir: str):
        self._base = base_dir.rstrip("/")
        self._w = None

    @property
    def w(self):
        if self._w is None:
            from backend.config import workspace_client

            self._w = workspace_client()
            try:
                self._w.files.create_directory(self._base)
            except Exception:
                pass
        return self._w

    def _path(self, pid: str) -> str:
        return f"{self._base}/{pid}.json"

    def list(self) -> list[ProjectSummary]:
        out: list[ProjectSummary] = []
        try:
            entries = self.w.files.list_directory_contents(self._base)
        except Exception:
            return out
        for e in entries:
            if not e.path.endswith(".json"):
                continue
            try:
                data = self.w.files.download(e.path).contents.read()
                out.append(to_summary(Project.model_validate_json(data)))
            except Exception:
                continue
        return sorted(out, key=lambda s: s.updated_at, reverse=True)

    def get(self, project_id: str) -> Project | None:
        try:
            data = self.w.files.download(self._path(project_id)).contents.read()
            return Project.model_validate_json(data)
        except Exception:
            return None

    def save(self, project: Project) -> None:
        import io

        body = project.model_dump_json(indent=2).encode("utf-8")
        self.w.files.upload(self._path(project.id), io.BytesIO(body), overwrite=True)

    def delete(self, project_id: str) -> None:
        try:
            self.w.files.delete(self._path(project_id))
        except Exception:
            pass


class PostgresStore(ProjectStore):
    """Stores projects in a Lakebase (Postgres) table — durable across App
    redeploys without needing a UC volume.

    One row per project: ``id uuid primary key, data jsonb, updated_at``. The id
    is a native ``uuid``; the full project (including its id as a string) lives in
    ``data``. The connection is app-level (its own credentials, resolved once),
    independent of any per-migration target a project points at — the store must
    work before any project (and its password) is loaded. The password is read
    from the project's Databricks secret scope at runtime, never embedded.
    """

    def __init__(self, *, host: str, database: str, user: str, port: int,
                 password: str, sslmode: str = "require", table: str = "lbx_projects"):
        self._conn_kwargs = dict(
            host=host, dbname=database, user=user, password=password,
            port=port, sslmode=sslmode, connect_timeout=15,
            application_name="lakebase-express-store",
        )
        # Identifier is from config, not user input; keep it simple and quote it.
        self._table = '"' + table.replace('"', "") + '"'
        self._ensured = False

    def _connect(self):
        import psycopg

        conn = psycopg.connect(**self._conn_kwargs)
        if not self._ensured:
            with conn.cursor() as cur:
                cur.execute(
                    f"CREATE TABLE IF NOT EXISTS {self._table} ("
                    "id UUID PRIMARY KEY, data JSONB NOT NULL, "
                    "updated_at TIMESTAMPTZ NOT NULL DEFAULT now())"
                )
            conn.commit()
            self._ensured = True
        return conn

    def list(self) -> list[ProjectSummary]:
        from psycopg.rows import dict_row

        out: list[ProjectSummary] = []
        with self._connect() as conn, conn.cursor(row_factory=dict_row) as cur:
            cur.execute(f"SELECT data FROM {self._table}")
            for row in cur.fetchall():
                try:
                    out.append(to_summary(Project.model_validate(row["data"])))
                except Exception:
                    continue  # ignore corrupt/foreign rows
        return sorted(out, key=lambda s: s.updated_at, reverse=True)

    @staticmethod
    def _as_uuid(project_id: str) -> str | None:
        """Validate the id is a real UUID before it reaches a ``::uuid`` cast, so
        a malformed path id reads as 'no such project' (404) instead of a 500."""
        try:
            return str(uuid.UUID(project_id))
        except (ValueError, AttributeError, TypeError):
            return None

    def get(self, project_id: str) -> Project | None:
        pid = self._as_uuid(project_id)
        if pid is None:
            return None
        with self._connect() as conn, conn.cursor() as cur:
            # id is a native uuid column; the app-level id is a string, so cast.
            cur.execute(f"SELECT data FROM {self._table} WHERE id = %s::uuid", (pid,))
            row = cur.fetchone()
            return Project.model_validate(row[0]) if row else None

    def save(self, project: Project) -> None:
        pid = self._as_uuid(project.id)
        if pid is None:
            raise ValueError(f"Project id is not a valid UUID: {project.id!r}")
        # model_dump(mode="json") so nested datetimes/enums serialize to JSON.
        payload = json.dumps(project.model_dump(mode="json"))
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                f"INSERT INTO {self._table} (id, data, updated_at) VALUES (%s::uuid, %s::jsonb, now()) "
                "ON CONFLICT (id) DO UPDATE SET data = EXCLUDED.data, updated_at = now()",
                (pid, payload),
            )
            conn.commit()

    def delete(self, project_id: str) -> None:
        pid = self._as_uuid(project_id)
        if pid is None:
            return
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(f"DELETE FROM {self._table} WHERE id = %s::uuid", (pid,))
            conn.commit()


def _resolve_store_password() -> str:
    """Password for the Postgres-backed store's app-level connection.

    Prefers ``LBX_PROJECTS_PG_PASSWORD``; otherwise reads it from the Databricks
    secret scope (``LBX_PROJECTS_PG_SECRET_SCOPE`` / ``_SECRET_KEY``, defaulting to
    the project scope + ``lakebase-password``) via the SDK — never embedded."""
    direct = os.getenv("LBX_PROJECTS_PG_PASSWORD")
    if direct:
        return direct
    scope = os.getenv("LBX_PROJECTS_PG_SECRET_SCOPE") or os.getenv("LBX_SECRET_SCOPE", "lakebase-express")
    key = os.getenv("LBX_PROJECTS_PG_SECRET_KEY", "lakebase-password")
    import base64

    from backend.config import workspace_client

    resp = workspace_client().secrets.get_secret(scope=scope, key=key)
    return base64.b64decode(resp.value).decode("utf-8") if resp.value else ""


@functools.lru_cache(maxsize=1)
def get_store() -> ProjectStore:
    backend = os.getenv("LBX_PROJECTS_BACKEND", "local").lower()
    if backend == "postgres":
        return PostgresStore(
            host=os.environ["LBX_PROJECTS_PG_HOST"],
            database=os.getenv("LBX_PROJECTS_PG_DATABASE", "databricks_postgres"),
            user=os.environ["LBX_PROJECTS_PG_USER"],
            port=int(os.getenv("LBX_PROJECTS_PG_PORT", "5432")),
            password=_resolve_store_password(),
            table=os.getenv("LBX_PROJECTS_PG_TABLE", "lbx_projects"),
        )
    directory = os.getenv("LBX_PROJECTS_DIR", str(Path.home() / ".lakebase-express" / "projects"))
    if backend == "volume":
        return VolumeStore(directory)
    return LocalFileStore(directory)
