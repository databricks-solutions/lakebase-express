"""Deterministic trigger-DDL sanitizer (schema-qualified name + idempotency)."""
from backend.schema_migration.trigger_sql import sanitize_trigger_sql


def test_strips_schema_from_trigger_name_keeps_function():
    # Exactly the SQL that failed in production: the function IS schema-qualified
    # (correct), only the trigger name must lose its schema.
    raw = ("CREATE OR REPLACE FUNCTION trading.trg_order_audit() RETURNS TRIGGER AS $$ "
           "BEGIN RETURN NEW; END; $$ LANGUAGE plpgsql; "
           "CREATE TRIGGER trading.trg_order_audit AFTER INSERT OR UPDATE ON trading.\"order\" "
           "FOR EACH ROW EXECUTE FUNCTION trading.trg_order_audit();")
    out = sanitize_trigger_sql(raw)
    assert "CREATE OR REPLACE TRIGGER trg_order_audit AFTER" in out
    assert "TRIGGER trading.trg_order_audit AFTER" not in out   # schema stripped from the name
    assert "FUNCTION trading.trg_order_audit()" in out          # function qualifier preserved
    assert "EXECUTE FUNCTION trading.trg_order_audit()" in out
    assert 'ON trading."order"' in out                          # ON clause untouched


def test_handles_quoted_and_plain_names():
    quoted = 'CREATE TRIGGER "custody"."trg_guard" AFTER UPDATE ON custody.cashbalance ...'
    assert 'CREATE OR REPLACE TRIGGER "trg_guard" AFTER' in sanitize_trigger_sql(quoted)
    plain = "CREATE TRIGGER trg_x BEFORE DELETE ON ref.instrument ..."
    # Already-unqualified name is left as-is (only upgraded to OR REPLACE).
    assert sanitize_trigger_sql(plain).startswith("CREATE OR REPLACE TRIGGER trg_x BEFORE")


def test_upgrades_plain_create_for_idempotent_reruns():
    # A trigger committed by a prior partial run must not fail the re-run with
    # "already exists" — plain CREATE TRIGGER becomes CREATE OR REPLACE TRIGGER.
    out = sanitize_trigger_sql("CREATE TRIGGER t AFTER INSERT ON s.x ...")
    assert out.count("CREATE OR REPLACE TRIGGER") == 1
    assert "CREATE TRIGGER" not in out.replace("CREATE OR REPLACE TRIGGER", "")


def test_does_not_double_up_or_replace_or_touch_constraint_triggers():
    already = "CREATE OR REPLACE TRIGGER t AFTER INSERT ON s.x ..."
    assert sanitize_trigger_sql(already).count("OR REPLACE") == 1
    # CONSTRAINT TRIGGER can't take OR REPLACE — leave the CREATE verb alone
    # (only the schema qualifier on the name is stripped).
    constraint = "CREATE CONSTRAINT TRIGGER app.chk AFTER INSERT ON app.x ..."
    out = sanitize_trigger_sql(constraint)
    assert out.startswith("CREATE CONSTRAINT TRIGGER chk AFTER")
    assert "OR REPLACE" not in out
