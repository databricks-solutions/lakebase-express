"""Callable shape: does the source hand rows back, and what did the translation make.

What these protect against is a total outage of one feature rather than a subtle
defect: Postgres rejects a mismatched call outright, so an application told to
`SELECT * FROM` a procedure — or to `CALL` a function — fails with SQLSTATE 42809 on
every single request through that call site.
"""
from backend.assessment import callable_shape as cs

# A report procedure: scratch table, several passes, then a trailing SELECT that
# hands the rows to its caller.
ROW_RETURNING = """
CREATE OR ALTER PROCEDURE dbo.usp_ItemReport
    @Category NVARCHAR(64) = NULL
AS
BEGIN
    SET NOCOUNT ON;

    CREATE TABLE #Scratch (ItemId INT NOT NULL PRIMARY KEY, ShortBy INT NULL);

    INSERT #Scratch (ItemId)
    SELECT p.ItemId
    FROM dbo.Stock AS i
    JOIN dbo.Items AS p ON p.ItemId = i.ItemId
    WHERE i.OnHand < i.Threshold
      AND (@Category IS NULL OR p.Category = @Category);

    UPDATE r
       SET ShortBy = ps.MinLead
    FROM #Scratch AS r
    CROSS APPLY (SELECT MIN(x.LeadTimeDays) AS MinLead
                 FROM dbo.Vendors AS x
                 WHERE x.ItemId = r.ItemId) AS ps;

    SELECT ItemId, ShortBy FROM #Scratch ORDER BY ShortBy DESC;

    DROP TABLE #Scratch;
END
"""

WRITE_ONLY = """
CREATE OR ALTER PROCEDURE dbo.usp_ApplyPayment @OrderId INT, @Amount MONEY
AS
BEGIN
    IF NOT EXISTS (SELECT 1 FROM dbo.Orders WHERE OrderId = @OrderId)
        THROW 50000, 'no such order', 1;

    DECLARE @Total MONEY;
    SELECT @Total = SUM(Amount) FROM dbo.Payments WHERE OrderId = @OrderId;

    INSERT dbo.Payments (OrderId, Amount)
    SELECT @OrderId, @Amount;

    UPDATE dbo.Orders SET Paid = 1 WHERE OrderId = @OrderId;
END
"""


# --- Does the source return a result set? -----------------------------------------


def test_a_trailing_select_is_a_result_set():
    assert cs.returns_result_set(ROW_RETURNING) is True


def test_a_write_only_procedure_returns_nothing():
    """None of `IF EXISTS (SELECT ...)`, `SELECT @x = ...` or `INSERT ... SELECT`
    hands rows to the caller — each is an obvious false positive to guard against."""
    assert cs.returns_result_set(WRITE_ONLY) is False


def test_select_into_is_not_a_result_set():
    body = "CREATE PROCEDURE dbo.p AS BEGIN SELECT ItemId INTO #t FROM dbo.Items; END"
    assert cs.returns_result_set(body) is False


def test_a_select_in_a_comment_or_string_is_not_a_result_set():
    body = """
    CREATE PROCEDURE dbo.p AS
    BEGIN
        -- SELECT * FROM dbo.Orders
        /* SELECT * FROM dbo.Items */
        EXEC('SELECT * FROM dbo.Customers');
    END
    """
    assert cs.returns_result_set(body) is False


def test_a_select_after_a_case_end_is_still_found():
    """A result set often follows `... END;` from a CASE expression."""
    body = """
    CREATE PROCEDURE dbo.p AS
    BEGIN
        UPDATE dbo.T SET Qty = CASE WHEN Qty < 1 THEN 1 ELSE Qty END;
        SELECT Id, Qty FROM dbo.T;
    END
    """
    assert cs.returns_result_set(body) is True


def test_an_empty_body_returns_nothing():
    assert cs.returns_result_set("") is False


# --- What did the translation create? ---------------------------------------------


def test_target_kind_reads_the_created_object():
    assert cs.target_kind("CREATE OR REPLACE PROCEDURE public.p() AS $$ $$;") == "procedure"
    assert cs.target_kind("CREATE FUNCTION public.f() RETURNS int AS $$ $$;") == "function"
    assert cs.target_kind("CREATE OR REPLACE VIEW public.v AS SELECT 1;") == "view"
    assert cs.target_kind("") == ""


def test_a_trigger_is_not_mistaken_for_its_companion_function():
    """A trigger translation creates the function first and the trigger second, so the
    last callable created is the one the catalog exposes as the trigger."""
    sql = (
        "CREATE OR REPLACE FUNCTION public.trg_fn() RETURNS trigger AS $$ $$;\n"
        "CREATE OR REPLACE TRIGGER trg AFTER INSERT ON public.t "
        "FOR EACH ROW EXECUTE FUNCTION public.trg_fn();"
    )
    assert cs.target_kind(sql) == "trigger"


def test_returns_set_distinguishes_a_table_function_from_a_scalar_one():
    assert cs.returns_set("CREATE FUNCTION f() RETURNS TABLE(a int) AS $$ $$;") is True
    assert cs.returns_set("CREATE FUNCTION f() RETURNS SETOF orders AS $$ $$;") is True
    assert cs.returns_set("CREATE FUNCTION f() RETURNS integer AS $$ $$;") is False


# --- The mismatch ------------------------------------------------------------------


def test_a_row_returning_procedure_left_as_a_procedure_cannot_serve_its_caller():
    target = "CREATE OR REPLACE PROCEDURE public.usp_itemreport(p text) AS $$ $$;"
    assert cs.serves_its_caller("PROCEDURE", ROW_RETURNING, target) is False
    assert "42809" in cs.shape_regression("PROCEDURE", ROW_RETURNING, target)


def test_reshaping_it_into_a_function_resolves_the_mismatch():
    target = (
        "CREATE OR REPLACE FUNCTION public.usp_itemreport(p text) "
        "RETURNS TABLE(item_id int) AS $$ BEGIN RETURN QUERY SELECT 1; END $$;"
    )
    assert cs.serves_its_caller("PROCEDURE", ROW_RETURNING, target) is True
    assert cs.shape_regression("PROCEDURE", ROW_RETURNING, target) == ""


def test_a_refcursor_procedure_is_accepted():
    """Rows come back through a cursor the caller FETCHes, which is a working shape
    even though the procedure itself returns nothing."""
    target = (
        "CREATE OR REPLACE PROCEDURE public.usp_ordersummary("
        "p_id int, INOUT header refcursor, INOUT lines refcursor) AS $$ $$;"
    )
    assert cs.serves_its_caller("PROCEDURE", ROW_RETURNING, target) is True


def test_a_write_only_procedure_is_never_flagged():
    target = "CREATE OR REPLACE PROCEDURE public.usp_applypayment(a int, b numeric) AS $$ $$;"
    assert cs.serves_its_caller("PROCEDURE", WRITE_ONLY, target) is True


def test_an_untranslated_object_is_not_flagged():
    assert cs.serves_its_caller("PROCEDURE", ROW_RETURNING, "") is True


def test_only_procedures_are_checked():
    """A source FUNCTION returning rows is already a function in the target."""
    assert cs.serves_its_caller("FUNCTION", ROW_RETURNING, "CREATE FUNCTION f() ...") is True


# --- Shape is read from statements, not from prose inside them ----------------------

RESHAPED_WITH_PROSE = """CREATE OR REPLACE FUNCTION public.usp_Rebuild()
RETURNS TABLE(product_id int, units bigint) LANGUAGE plpgsql AS $$
BEGIN
  -- Was: CREATE PROCEDURE dbo.usp_Rebuild rebuilding a ##global temp table.
  RETURN QUERY SELECT 1, 2::bigint;
END
$$;"""


def test_a_create_procedure_in_a_body_comment_does_not_decide_the_kind():
    """One token in prose reported a correctly migrated object as both the wrong kind
    and duplicated — two false alarms from a misread comment."""
    assert cs.target_kind(RESHAPED_WITH_PROSE) == "function"
    assert cs.returns_set(RESHAPED_WITH_PROSE) is True


def test_such_an_object_serves_its_caller():
    source = "CREATE PROCEDURE dbo.usp_Rebuild AS BEGIN SELECT 1 FROM dbo.T; END"
    assert cs.serves_its_caller("PROCEDURE", source, RESHAPED_WITH_PROSE) is True
    assert cs.shape_regression("PROCEDURE", source, RESHAPED_WITH_PROSE) == ""


def test_statements_only_keeps_the_signature_and_blanks_the_body():
    stripped = cs.statements_only(RESHAPED_WITH_PROSE)
    assert "RETURNS TABLE" in stripped          # signature survives
    assert "CREATE PROCEDURE" not in stripped   # body comment does not
    assert "RETURN QUERY" not in stripped
    assert len(stripped) == len(RESHAPED_WITH_PROSE)   # offsets preserved


def test_a_dollar_tag_body_is_handled():
    sql = ("CREATE OR REPLACE PROCEDURE public.p() LANGUAGE plpgsql AS $body$ "
           "BEGIN -- CREATE FUNCTION decoy() \n END $body$;")
    assert cs.target_kind(sql) == "procedure"


def test_a_comment_containing_a_dollar_quote_does_not_swallow_the_body():
    sql = ("CREATE OR REPLACE VIEW public.v AS -- note the $$ in this comment\n"
           "SELECT 1;")
    assert cs.target_kind(sql) == "view"


def test_a_cursor_declared_only_in_the_body_is_not_a_caller_visible_refcursor():
    body_only = ("CREATE FUNCTION f() RETURNS int LANGUAGE plpgsql AS $$ "
                 "DECLARE c refcursor; BEGIN RETURN 1; END $$;")
    parameter = ("CREATE OR REPLACE PROCEDURE public.s(INOUT header refcursor) "
                 "LANGUAGE plpgsql AS $$ BEGIN OPEN header FOR SELECT 1; END $$;")
    assert cs.uses_refcursor(body_only) is False
    assert cs.uses_refcursor(parameter) is True


# --- What a translation creates -----------------------------------------------------


def test_created_objects_lists_helpers_alongside_the_main_object():
    sql = (
        'CREATE UNLOGGED TABLE IF NOT EXISTS public."ReorderScratch" (run_id uuid);\n'
        "CREATE TYPE public.reorder_row AS (item_id int, short_by int);\n"
        "CREATE OR REPLACE FUNCTION public.usp_ItemReport(p text) RETURNS TABLE(a int)\n"
        "LANGUAGE plpgsql AS $$\nBEGIN\n"
        "  -- decoy: CREATE TABLE public.not_real (x int)\n"
        "  RETURN QUERY SELECT 1;\nEND $$;"
    )
    made = cs.created_objects(sql)

    assert ("public", "ReorderScratch") in made      # quoted, so case kept
    assert ("public", "reorder_row") in made
    assert ("public", "usp_itemreport") in made      # unquoted, so folded
    assert ("public", "not_real") not in made        # named only in a body comment


def test_created_objects_falls_back_to_the_target_schema():
    assert cs.created_objects("CREATE TABLE scratch (a int);", "app") == {("app", "scratch")}


def test_created_objects_covers_the_trigger_pair():
    sql = ("CREATE OR REPLACE FUNCTION public.trg_fn() RETURNS trigger AS $$ BEGIN "
           "RETURN NEW; END $$;\n"
           "CREATE OR REPLACE TRIGGER trg AFTER INSERT ON public.t FOR EACH ROW "
           "EXECUTE FUNCTION public.trg_fn();")
    made = cs.created_objects(sql)

    assert ("public", "trg_fn") in made and ("public", "trg") in made


def test_created_objects_is_empty_for_nothing():
    assert cs.created_objects("") == set()
    assert cs.created_objects("SELECT 1;") == set()
