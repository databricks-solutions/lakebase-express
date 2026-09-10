"""Snapshot job provisioning — exercised with a fake workspace client, no live workspace."""
from types import SimpleNamespace

from backend.data_migration.models import DataGenRequest, PostLoadStatement, TableRef
from backend.migration import async_setup, job_offload


def _req(post_load_sql=None, project_id=""):
    return DataGenRequest(
        host="h", database="db", username="u", password_secret_key="k",
        lakebase_host="lb-host", lakebase_user="lbuser",
        lakebase_password_secret_key="lb-key",
        tables=[TableRef(schema_name="dbo", table_name="Orders")],
        post_load_sql=post_load_sql or [],
        project_id=project_id,
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
    def __init__(self, existing_job_id: int | None, existing_name: str | None = None,
                 existing_tags: dict | None = None):
        self.existing_job_id = existing_job_id
        self.existing_name = existing_name or job_offload.JOB_NAME
        self.existing_tags = existing_tags or {}
        self.listed_names: list[str | None] = []
        self.created_with: dict | None = None
        self.reset_with: dict | None = None
        self.run_now_job_ids: list[int] = []

    def list(self, name=None):
        self.listed_names.append(name)
        if self.existing_job_id is None:
            return
        # No name filter: the tag-matching fallback enumerating the workspace.
        if name is None or name == self.existing_name:
            yield SimpleNamespace(
                job_id=self.existing_job_id,
                settings=SimpleNamespace(tags=self.existing_tags),
            )

    def create(self, name=None, tasks=None, schedule=None, tags=None):
        self.created_with = {"name": name, "tasks": tasks, "schedule": schedule,
                             "tags": tags}
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
        self.contents: dict[str, str] = {}

    def mkdirs(self, path):
        pass

    def delete(self, path):
        self.deleted.append(path)

    def upload(self, path, content, format=None, language=None, overwrite=False):
        self.uploaded.append(path)
        self.contents[path] = content.decode("utf-8")


class _FakeClient:
    def __init__(self, existing_job_id: int | None, existing_name: str | None = None,
                 existing_tags: dict | None = None):
        self.jobs = _FakeJobs(existing_job_id, existing_name, existing_tags)
        self.workspace = _FakeWorkspaceOps()
        self.config = SimpleNamespace(host="https://ws.example.com")


def _patch(monkeypatch, existing_job_id: int | None = None,
           existing_name: str | None = None,
           existing_tags: dict | None = None) -> _FakeClient:
    client = _FakeClient(existing_job_id, existing_name, existing_tags)
    monkeypatch.setattr(job_offload, "workspace_client", lambda: client)
    monkeypatch.setattr(async_setup, "workspace_client", lambda: client)
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


PROJECT_A = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
PROJECT_B = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"


def _named(monkeypatch, **names):
    """Resolve project names without touching the real project store."""
    monkeypatch.setattr(job_offload, "project_name", lambda pid: names.get(pid, ""))


def test_the_job_is_titled_by_its_project(monkeypatch):
    """Readability wins over uniqueness here — the id lives in the tags instead."""
    _named(monkeypatch, **{PROJECT_A: "Sales  ERP"})
    assert job_offload.job_name(PROJECT_A) == "lakebase-express-snapshot · Sales ERP"
    assert PROJECT_A not in job_offload.job_name(PROJECT_A)
    # With no resolvable name the id is all that is left to tell projects apart.
    assert job_offload.job_name(PROJECT_B) == f"lakebase-express-snapshot ({PROJECT_B})"


def test_the_job_is_tagged_with_the_project(monkeypatch):
    client = _patch(monkeypatch)
    _named(monkeypatch, **{PROJECT_A: "Sales ERP"})
    job_offload.create_scheduled_job(_req(project_id=PROJECT_A), "/W/lbx")

    assert client.jobs.created_with["tags"] == {
        "lbx_project_id": PROJECT_A, "lbx_project": "Sales ERP",
    }


def test_tag_values_drop_characters_the_cloud_provider_rejects(monkeypatch):
    _named(monkeypatch, **{PROJECT_A: "Sales <ERP> 50%/yr?"})
    tags = job_offload.job_tags(PROJECT_A)
    assert tags["lbx_project"] == "Sales -ERP- 50-/yr-"
    assert not set(tags["lbx_project"]) & set("<>%&?\\")


def test_an_unnamed_project_is_still_tagged_with_its_id(monkeypatch):
    _named(monkeypatch)
    assert job_offload.job_tags(PROJECT_A) == {"lbx_project_id": PROJECT_A}
    assert job_offload.job_tags("") == {}


def test_renaming_a_project_reuses_its_job_instead_of_duplicating_it(monkeypatch):
    """The name is part of the job's title, so a rename stops it matching. Matching
    the tag as well keeps one job per project — a duplicate would carry its own
    schedule and refresh the same tables."""
    client = _patch(monkeypatch, existing_job_id=77,
                    existing_name="lakebase-express-snapshot · Old",
                    existing_tags={"lbx_project_id": PROJECT_A, "lbx_project": "Old"})
    _named(monkeypatch, **{PROJECT_A: "New"})
    out = job_offload.create_scheduled_job(_req(project_id=PROJECT_A), "/W/lbx")

    assert out["job_id"] == 77 and out["job_created"] is False
    assert client.jobs.created_with is None
    # Reused and retitled, not duplicated.
    settings = client.jobs.reset_with["new_settings"]
    assert settings.name == "lakebase-express-snapshot · New"
    assert settings.tags["lbx_project"] == "New"


def test_the_tag_fallback_never_reaches_across_projects(monkeypatch):
    client = _patch(monkeypatch, existing_job_id=77,
                    existing_name="lakebase-express-snapshot · B",
                    existing_tags={"lbx_project_id": PROJECT_B})
    _named(monkeypatch, **{PROJECT_A: "A"})
    out = job_offload.create_scheduled_job(_req(project_id=PROJECT_A), "/W/lbx")
    assert out["job_created"] is True and client.jobs.reset_with is None


def test_two_projects_with_the_same_name_do_not_repoint_each_other(monkeypatch):
    """Titles are no longer unique, so the name match alone would hand project B the
    job project A is about to run — the collision the per-project split fixed."""
    same = "lakebase-express-snapshot · Sales"
    client = _patch(monkeypatch, existing_job_id=77, existing_name=same,
                    existing_tags={"lbx_project_id": PROJECT_A, "lbx_project": "Sales"})
    _named(monkeypatch, **{PROJECT_A: "Sales", PROJECT_B: "Sales"})
    out = job_offload.create_scheduled_job(_req(project_id=PROJECT_B), "/W/lbx")

    assert out["job_created"] is True and client.jobs.reset_with is None
    # Same title, different job, told apart by the tag.
    assert client.jobs.created_with["name"] == same
    assert client.jobs.created_with["tags"]["lbx_project_id"] == PROJECT_B


def test_a_project_still_reuses_its_own_same_named_job(monkeypatch):
    same = "lakebase-express-snapshot · Sales"
    client = _patch(monkeypatch, existing_job_id=77, existing_name=same,
                    existing_tags={"lbx_project_id": PROJECT_A, "lbx_project": "Sales"})
    _named(monkeypatch, **{PROJECT_A: "Sales"})
    out = job_offload.create_scheduled_job(_req(project_id=PROJECT_A), "/W/lbx")

    assert out["job_id"] == 77 and out["job_created"] is False
    assert client.jobs.created_with is None


def test_an_unresolvable_project_name_does_not_fail_provisioning(monkeypatch):
    """The name is decoration; losing it must not cost the user a job."""
    from backend.projects import store as project_store

    client = _patch(monkeypatch)

    def boom():
        raise RuntimeError("project store is down")

    monkeypatch.setattr(project_store, "get_store", boom)
    out = job_offload.create_scheduled_job(_req(project_id=PROJECT_A), "/W/lbx")
    assert out["job_id"] == 100
    assert client.jobs.created_with["name"] == f"lakebase-express-snapshot ({PROJECT_A})"


def test_each_project_gets_its_own_job_and_notebook_folder(monkeypatch):
    """Two projects provisioned in a row used to share one job AND one notebook
    folder, so the second silently repointed the first at its own load — the bug
    only shows up when the first project is finally run."""
    a = _patch(monkeypatch)
    job_offload.create_scheduled_job(_req(project_id=PROJECT_A), "/W/lbx")
    b = _patch(monkeypatch)
    job_offload.create_scheduled_job(_req(project_id=PROJECT_B), "/W/lbx")

    assert a.jobs.created_with["name"] != b.jobs.created_with["name"]
    assert PROJECT_A in a.jobs.created_with["name"]
    assert all(p.startswith(f"/W/lbx/{PROJECT_A}/") for p in a.workspace.uploaded)
    assert all(p.startswith(f"/W/lbx/{PROJECT_B}/") for p in b.workspace.uploaded)
    assert set(a.workspace.uploaded).isdisjoint(b.workspace.uploaded)


def test_one_project_provisioned_twice_reuses_its_own_job(monkeypatch):
    """Repeated setups must still manage one job per project, not pile up."""
    name = job_offload.job_name(PROJECT_A)
    client = _patch(monkeypatch, existing_job_id=77, existing_name=name)
    out = job_offload.create_scheduled_job(_req(project_id=PROJECT_A), "/W/lbx")

    assert out["job_id"] == 77 and out["job_created"] is False
    assert client.jobs.created_with is None
    assert client.jobs.reset_with["new_settings"].name == name
    # Looked the job up under its own name, never the shared one.
    assert client.jobs.listed_names == [name]


def test_another_projects_job_is_never_adopted(monkeypatch):
    client = _patch(monkeypatch, existing_job_id=77, existing_name=job_offload.job_name(PROJECT_B))
    out = job_offload.create_scheduled_job(_req(project_id=PROJECT_A), "/W/lbx")
    assert out["job_created"] is True and client.jobs.reset_with is None


def test_a_request_without_a_project_keeps_the_shared_job(monkeypatch):
    """Unscoped API clients are not migrated onto a new job behind their back."""
    client = _patch(monkeypatch, existing_job_id=77)
    out = job_offload.create_scheduled_job(_req(), "/W/lbx")
    assert out["job_id"] == 77
    assert client.jobs.reset_with["new_settings"].name == job_offload.JOB_NAME
    # No project subfolder either — the notebooks stay where they always were.
    assert client.workspace.uploaded[0] == "/W/lbx/01_snapshot_load"


def _recorded_states(monkeypatch):
    """Swap async_runs' registries for ones over a throwaway store, and return it."""
    from backend.migration import async_runs
    from backend.migration.models import AsyncRunState
    from backend.run_registry import RunRegistry
    from backend.run_store import MemoryRunStore

    store = MemoryRunStore()
    monkeypatch.setattr(async_runs, "_JOBS", RunRegistry("async_job", AsyncRunState, store=store))
    monkeypatch.setattr(async_runs, "_RUNS", RunRegistry("async_run", AsyncRunState, store=store))
    return store


def test_async_setup_records_the_run_so_it_appears_in_history(monkeypatch):
    # Async mode used to leave no trace in lbx_runs — its state lived only in the
    # Jobs UI, so history covered sync runs alone.
    _patch(monkeypatch)
    store = _recorded_states(monkeypatch)
    out = async_setup.setup_async(_req(), "/Workspace/Shared/x")

    records = store.list(kind="async_run")
    assert len(records) == 1 and records[0].status == "submitted"
    # The caller gets our run id alongside the Databricks one.
    assert out["lbx_run_id"] == records[0].run_id
    assert out["run_id"] == 555


def test_create_job_run_later_records_a_job_and_leaves_the_runs_to_the_notebooks(monkeypatch):
    """The 'create job, run later' path: nothing has executed yet, so there is no
    run to record — only the job. Its executions are recorded from inside the
    notebooks, however they are triggered."""
    _patch(monkeypatch)
    store = _recorded_states(monkeypatch)
    out = async_setup.setup_async(_req(), "/Workspace/Shared/x", run_now=False)

    assert [r.status for r in store.list(kind="async_job")] == ["created"]
    assert store.list(kind="async_run") == []
    assert out["lbx_run_id"] == store.list(kind="async_job")[0].run_id


def test_create_job_run_later_still_grants_and_wires_reporting(monkeypatch):
    """Whatever the app records, the job must be able to report for itself: the
    notebooks carry the coordinates and the run-as identity holds the grant before
    anyone presses Run now."""
    from backend.migration import async_setup as mod

    client = _patch(monkeypatch)
    _recorded_states(monkeypatch)
    seen: dict = {}
    monkeypatch.setattr(mod, "get_run_store", lambda: _StoreWithNotebookConfig(seen))
    monkeypatch.setattr(mod, "run_as_identity", lambda w, job_id: "sp-123")
    out = mod.setup_async(_req(), "/Workspace/Shared/x", run_now=False)

    assert "run_state_warning" not in out
    # The grant lands at provisioning, not at run time — nothing runs later to do it.
    assert seen["granted"] == "sp-123"
    loader = client.workspace.contents[client.workspace.uploaded[0]]
    assert "generate_database_credential" in loader
    assert '_report_run_state("running")' in loader
    # Resolved per run, so each Run now writes its own row.
    for task in client.jobs.created_with["tasks"]:
        assert task.notebook_task.base_parameters["job_run_id"] == "{{job.run_id}}"


def test_a_job_that_cannot_report_says_so_at_provisioning(monkeypatch):
    """Without a run store the notebooks are generated inert, and no later run can
    fix that — so the one moment to say it is now."""
    from backend.migration import async_setup as mod

    _patch(monkeypatch)
    _recorded_states(monkeypatch)
    monkeypatch.setattr(mod, "get_run_store", lambda: _StoreWithoutNotebookConfig())
    out = mod.setup_async(_req(), "/Workspace/Shared/x", run_now=False)

    assert "will not be recorded" in out["run_state_warning"]
    assert out["job_id"] == 100  # provisioning still succeeded


def test_async_setup_schedule_records_a_scheduled_run(monkeypatch):
    _patch(monkeypatch)
    store = _recorded_states(monkeypatch)
    async_setup.setup_async(_req(), "/Workspace/Shared/x", "0 0 * * * ?", "UTC")
    assert [r.status for r in store.list(kind="async_job")] == ["scheduled"]
    assert store.list(kind="async_run") == []


def test_a_failed_recording_does_not_fail_the_provisioning(monkeypatch):
    # The job exists in the workspace by this point; losing the bookkeeping row
    # must not turn a successful provision into an error.
    from backend.migration import async_runs

    _patch(monkeypatch)
    def boom(*a, **k):
        raise RuntimeError("run store is down")

    monkeypatch.setattr(async_runs, "record", boom)
    out = async_setup.setup_async(_req(), "/Workspace/Shared/x")
    assert out["job_id"] == 100 and "lbx_run_id" not in out


def test_async_setup_schedule_wins_over_run_now(monkeypatch):
    client = _patch(monkeypatch)
    out = async_setup.setup_async(_req(), "/Workspace/Shared/x", "0 0 * * * ?", "UTC", run_now=True)
    assert client.jobs.run_now_job_ids == []
    assert out["scheduled"] is True


# --- Run-state wiring (OAuth) ------------------------------------------------------


def test_tasks_pass_the_job_run_id_so_every_task_shares_a_run_id(monkeypatch):
    client = _patch(monkeypatch)
    async_setup.setup_async(_req_with_post(), "/Workspace/Shared/x")
    params = [t.notebook_task.base_parameters for t in client.jobs.created_with["tasks"]]
    assert params and all(
        p == {"job_id": "{{job.id}}", "job_run_id": "{{job.run_id}}"} for p in params
    )


def test_run_store_coordinates_are_filled_server_side(monkeypatch):
    """The client never supplies them — they are the app's own config."""
    from backend.data_migration.models import RunStoreTarget
    from backend.migration import async_setup as mod

    seen = {}
    monkeypatch.setattr(mod, "get_run_store", lambda: _StoreWithNotebookConfig(seen))
    req = _req()
    assert req.run_store is None
    out = mod.with_run_store(req)
    assert isinstance(out.run_store, RunStoreTarget)
    assert out.run_store.endpoint == "projects/p/branches/production/endpoints/primary"


def test_a_failed_grant_is_surfaced_not_swallowed(monkeypatch):
    # Discovering this later as a missing history row is the failure mode we are
    # avoiding, so provisioning reports it while it can still be fixed.
    from backend.migration import async_setup as mod

    _patch(monkeypatch)
    monkeypatch.setattr(mod, "get_run_store", lambda: _StoreThatRefusesGrants())
    monkeypatch.setattr(mod, "run_as_identity", lambda w, job_id: "sp-123")
    out = mod.setup_async(_req(), "/Workspace/Shared/x")

    assert "sp-123" in out["run_state_warning"]
    assert "not appear in run history" in out["run_state_warning"]
    assert out["job_id"] == 100  # provisioning still succeeded


class _StoreWithNotebookConfig:
    def __init__(self, seen):
        self._seen = seen

    def notebook_config(self):
        return {"host": "ep-x", "port": 5432, "database": "databricks_postgres",
                "table": "lbx_runs",
                "endpoint": "projects/p/branches/production/endpoints/primary"}

    def grant_writer(self, identity):
        self._seen["granted"] = identity


class _StoreWithoutNotebookConfig(_StoreWithNotebookConfig):
    def __init__(self):
        super().__init__({})

    def notebook_config(self):
        return None


class _StoreThatRefusesGrants(_StoreWithNotebookConfig):
    def __init__(self):
        super().__init__({})

    def grant_writer(self, identity):
        raise RuntimeError("role does not exist")
