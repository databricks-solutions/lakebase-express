"""Snapshot job provisioning — exercised with a fake workspace client, no live workspace."""
from types import SimpleNamespace

from backend.data_migration.models import DataGenRequest, PostLoadStatement, TableRef
from backend.migration import async_setup, job_offload


def _req(post_load_sql=None):
    return DataGenRequest(
        host="h", database="db", username="u", password_secret_key="k",
        lakebase_host="lb-host", lakebase_user="lbuser",
        lakebase_password_secret_key="lb-key",
        tables=[TableRef(schema_name="dbo", table_name="Orders")],
        post_load_sql=post_load_sql or [],
    )


# A request whose post-data phase covers constraints, indexes, and triggers
# (no FKs) — so the job gets those three post-load tasks, chained after load.
def _req_with_post():
    return _req(post_load_sql=[
        PostLoadStatement(name="pk", kind="constraint", sql="ALTER TABLE x ADD PRIMARY KEY (id);"),
        PostLoadStatement(name="idx", kind="index", sql="CREATE INDEX IF NOT EXISTS i ON x (id);"),
        PostLoadStatement(name="trg", kind="trigger",
                          sql="CREATE TRIGGER s.t AFTER INSERT ON s.x FOR EACH ROW EXECUTE FUNCTION s.t();"),
    ])


class _FakeJobs:
    def __init__(self, existing_job_id: int | None):
        self.existing_job_id = existing_job_id
        self.created_with: dict | None = None
        self.reset_with: dict | None = None
        self.run_now_job_ids: list[int] = []

    def list(self, name=None):
        if self.existing_job_id is not None and name == job_offload.JOB_NAME:
            yield SimpleNamespace(job_id=self.existing_job_id)

    def create(self, name=None, tasks=None, schedule=None):
        self.created_with = {"name": name, "tasks": tasks, "schedule": schedule}
        return SimpleNamespace(job_id=100)

    def reset(self, job_id=None, new_settings=None):
        self.reset_with = {"job_id": job_id, "new_settings": new_settings}

    def run_now(self, job_id=None):
        self.run_now_job_ids.append(job_id)
        return SimpleNamespace(run_id=555)


class _FakeWorkspaceOps:
    def __init__(self):
        self.uploaded: list[str] = []
        self.deleted: list[str] = []

    def mkdirs(self, path):
        pass

    def delete(self, path):
        self.deleted.append(path)

    def upload(self, path, content, format=None, language=None, overwrite=False):
        self.uploaded.append(path)


class _FakeClient:
    def __init__(self, existing_job_id: int | None):
        self.jobs = _FakeJobs(existing_job_id)
        self.workspace = _FakeWorkspaceOps()
        self.config = SimpleNamespace(host="https://ws.example.com")


def _patch(monkeypatch, existing_job_id: int | None = None) -> _FakeClient:
    client = _FakeClient(existing_job_id)
    monkeypatch.setattr(job_offload, "workspace_client", lambda: client)
    return client


def test_upload_deletes_stale_notebook_first(monkeypatch):
    """Overwrite only replaces source — sidecar metadata (serverless environment
    panel) survives and is installed before any cell runs. Delete, then upload."""
    client = _patch(monkeypatch)
    job_offload.create_job_and_run(_req_with_post(), "/Workspace/Shared/x")
    assert client.workspace.deleted == client.workspace.uploaded
    assert client.workspace.uploaded == [
        "/Workspace/Shared/x/01_snapshot_load",
        "/Workspace/Shared/x/02_post_load_constraints",
        "/Workspace/Shared/x/03_post_load_indexes",
        "/Workspace/Shared/x/04_post_load_triggers",
    ]


def test_job_chains_one_task_per_post_load_type(monkeypatch):
    """Each post-data type is its own task, chained after the copy in dependency
    order, so a failure surfaces per type and each is independently repairable."""
    client = _patch(monkeypatch)
    job_offload.create_job_and_run(_req_with_post(), "/Workspace/Shared/x")
    tasks = client.jobs.created_with["tasks"]
    assert [t.task_key for t in tasks] == [
        "load", "post_load_constraints", "post_load_indexes", "post_load_triggers",
    ]
    # Linear chain: task N depends on task N-1; the first depends on nothing.
    assert tasks[0].depends_on is None
    for prev, cur in zip(tasks, tasks[1:]):
        assert [d.task_key for d in cur.depends_on] == [prev.task_key]
    assert tasks[-1].notebook_task.notebook_path == "/Workspace/Shared/x/04_post_load_triggers"


def test_job_is_single_task_when_no_post_data(monkeypatch):
    """No post-data items -> just the snapshot task, no dangling dependencies."""
    client = _patch(monkeypatch)
    job_offload.create_job_and_run(_req(), "/Workspace/Shared/x")
    tasks = client.jobs.created_with["tasks"]
    assert [t.task_key for t in tasks] == ["load"]
    assert tasks[0].depends_on is None


def test_one_off_creates_persistent_job_and_runs_it(monkeypatch):
    """A one-off snapshot must leave a re-runnable job behind, not a bare run."""
    client = _patch(monkeypatch)
    out = job_offload.create_job_and_run(_req(), "/Workspace/Shared/x")
    assert client.jobs.created_with["name"] == job_offload.JOB_NAME
    assert client.jobs.created_with["schedule"] is None
    assert client.jobs.run_now_job_ids == [100]
    assert out["job_id"] == 100 and out["job_created"] is True
    assert out["run_id"] == 555
    assert out["url"] == "https://ws.example.com/jobs/100"
    assert out["run_url"] == "https://ws.example.com/jobs/runs/555"


def test_one_off_reuses_existing_job_by_name(monkeypatch):
    """Re-clicking must repoint the existing job, not accumulate duplicates."""
    client = _patch(monkeypatch, existing_job_id=7)
    out = job_offload.create_job_and_run(_req(), "/Workspace/Shared/x")
    assert client.jobs.created_with is None
    assert client.jobs.reset_with["job_id"] == 7
    assert client.jobs.reset_with["new_settings"].schedule is None
    assert client.jobs.run_now_job_ids == [7]
    assert out["job_id"] == 7 and out["job_created"] is False


def test_scheduled_job_reuses_by_name_and_sets_cron(monkeypatch):
    client = _patch(monkeypatch, existing_job_id=7)
    out = job_offload.create_scheduled_job(_req(), "/Workspace/Shared/x", "0 0 * * * ?", "UTC")
    schedule = client.jobs.reset_with["new_settings"].schedule
    assert schedule.quartz_cron_expression == "0 0 * * * ?"
    assert out["job_id"] == 7 and out["scheduled"] is True
    assert client.jobs.run_now_job_ids == []  # scheduled path doesn't force a run


# --- Async setup (run now / create only / schedule) -------------------------------


def test_async_setup_run_now_submits_a_run(monkeypatch):
    client = _patch(monkeypatch)
    out = async_setup.setup_async(_req(), "/Workspace/Shared/x")
    assert client.jobs.run_now_job_ids == [100]
    assert out["run_id"] == 555 and out["scheduled"] is False


def test_async_setup_create_only_leaves_job_unstarted(monkeypatch):
    """run_now=False must create the persistent job but never trigger a run, so
    the user can pick/tune the compute in the Jobs UI before starting it."""
    client = _patch(monkeypatch)
    out = async_setup.setup_async(_req(), "/Workspace/Shared/x", run_now=False)
    assert client.jobs.created_with["name"] == job_offload.JOB_NAME
    assert client.jobs.created_with["schedule"] is None
    assert client.jobs.run_now_job_ids == []
    assert out["job_id"] == 100 and out["scheduled"] is False
    assert out["run_id"] is None and out["run_url"] is None
    assert "pick the compute" in out["note"]


def test_async_setup_schedule_wins_over_run_now(monkeypatch):
    client = _patch(monkeypatch)
    out = async_setup.setup_async(_req(), "/Workspace/Shared/x", "0 0 * * * ?", "UTC", run_now=True)
    assert client.jobs.run_now_job_ids == []
    assert out["scheduled"] is True
