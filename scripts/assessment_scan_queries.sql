/* =============================================================================
   Lakebase Express — assessment scan queries, exactly as the app issues them.
   Generated from backend/assessment/scanner.py; run as the app's source login.

   Purpose: find which query a restricted login fails on. Run the whole file at
   once; each SELECT is preceded by a marker so the output identifies itself.

   All read-only: nine SELECTs over catalog views, no writes, no data pages.
   Ordered as scanner.py runs them (1-8 in scan_tables, 9 in scan_objects), so
   the first error you hit is the one aborting the real scan.

   Permissions each query needs, if it fails or comes back empty:
     Q2  sys.dm_db_partition_stats -> GRANT VIEW DATABASE STATE TO [<login>];
     Q9  sys.sql_modules.definition -> GRANT VIEW DEFINITION    TO [<login>];
     others: metadata visibility, which db_datareader membership provides.

   NOTE catalog views (sys.*, INFORMATION_SCHEMA.*) are filtered by metadata
   visibility: with insufficient rights rows are silently OMITTED rather than
   raising. So a query returning 0 rows is as much a finding as one that errors.
   The DMV in Q2 is the exception — it raises outright.

   HOW TO RUN. In SSMS / Azure Data Studio the whole file is one batch, and a
   permission error on one statement does NOT stop the rest: you get the error
   in the Messages tab plus result grids for the queries that succeeded. That is
   what you want here — the point is to see every failure in one pass, not just
   the first. Check the Messages tab, not only the grids.

   With sqlcmd, add -r1 so errors go to stderr where you can see them:
       sqlcmd -S <server>.database.windows.net -d <db> -U <login> -P <pw> \
              -i assessment_scan_queries.sql -r1

   The real app is stricter: it runs these as nine separate statements and
   aborts on the first exception (backend/api/assessment_routes.py:85), so the
   earliest failure below is the one breaking the scan.
   ============================================================================= */

SET NOCOUNT OFF;


/* ---------------------------------------------------------------------------
   Q0 — connectivity probe (AzureSqlConnection.test_connection)
        The UI runs this before a scan. If this fails it is login/network,
        not permissions.
   --------------------------------------------------------------------------- */
PRINT '===== Q0 connectivity =====';
SELECT 1 AS ok;


/* ---------------------------------------------------------------------------
   Q1 — Table columns   [scanner.py _COLUMNS_SQL]
        structure: one row per column of every user table
        Needs: metadata visibility (db_datareader)
   --------------------------------------------------------------------------- */
PRINT '===== Q1 Table columns =====';
SELECT  c.TABLE_SCHEMA, c.TABLE_NAME, c.COLUMN_NAME, c.DATA_TYPE,
        c.CHARACTER_MAXIMUM_LENGTH, c.NUMERIC_PRECISION, c.NUMERIC_SCALE,
        c.IS_NULLABLE
FROM    INFORMATION_SCHEMA.COLUMNS c
JOIN    sys.tables t      ON t.name = c.TABLE_NAME
JOIN    sys.schemas s     ON s.schema_id = t.schema_id AND s.name = c.TABLE_SCHEMA
WHERE   t.is_ms_shipped = 0
  AND   c.TABLE_SCHEMA NOT IN ('sys', 'INFORMATION_SCHEMA', 'guest', 'db_owner', 'db_accessadmin', 'db_securityadmin', 'db_ddladmin', 'db_backupoperator', 'db_datareader', 'db_datawriter', 'db_denydatareader', 'db_denydatawriter');


/* ---------------------------------------------------------------------------
   Q2 — Approximate row counts   [scanner.py _ROWCOUNTS_SQL]
        cheap row counts from partition stats, avoiding COUNT(*) per table
        Needs: VIEW DATABASE STATE  <-- raises if missing; aborts the real scan here
   --------------------------------------------------------------------------- */
PRINT '===== Q2 Approximate row counts =====';
SELECT  s.name AS TABLE_SCHEMA, t.name AS TABLE_NAME,
        SUM(p.row_count) AS ROW_COUNT
FROM    sys.dm_db_partition_stats p
JOIN    sys.tables t  ON t.object_id = p.object_id
JOIN    sys.schemas s ON s.schema_id = t.schema_id
WHERE   p.index_id IN (0, 1)
  AND   t.is_ms_shipped = 0
  AND   s.name NOT IN ('sys', 'INFORMATION_SCHEMA', 'guest', 'db_owner', 'db_accessadmin', 'db_securityadmin', 'db_ddladmin', 'db_backupoperator', 'db_datareader', 'db_datawriter', 'db_denydatareader', 'db_denydatawriter')
GROUP BY s.name, t.name;


/* ---------------------------------------------------------------------------
   Q3 — Primary key columns   [scanner.py _PRIMARY_KEYS_SQL]
        PK columns in key order (composite keys keep their order)
        Needs: metadata visibility (db_datareader)
   --------------------------------------------------------------------------- */
PRINT '===== Q3 Primary key columns =====';
SELECT  kcu.TABLE_SCHEMA, kcu.TABLE_NAME, kcu.COLUMN_NAME, kcu.ORDINAL_POSITION
FROM    INFORMATION_SCHEMA.TABLE_CONSTRAINTS tc
JOIN    INFORMATION_SCHEMA.KEY_COLUMN_USAGE kcu
        ON  kcu.CONSTRAINT_NAME = tc.CONSTRAINT_NAME
        AND kcu.CONSTRAINT_SCHEMA = tc.CONSTRAINT_SCHEMA
WHERE   tc.CONSTRAINT_TYPE = 'PRIMARY KEY'
  AND   kcu.TABLE_SCHEMA NOT IN ('sys', 'INFORMATION_SCHEMA', 'guest', 'db_owner', 'db_accessadmin', 'db_securityadmin', 'db_ddladmin', 'db_backupoperator', 'db_datareader', 'db_datawriter', 'db_denydatareader', 'db_denydatawriter')
ORDER BY kcu.TABLE_SCHEMA, kcu.TABLE_NAME, kcu.ORDINAL_POSITION;


/* ---------------------------------------------------------------------------
   Q4 — Foreign keys   [scanner.py _FOREIGN_KEYS_SQL]
        one row per FK constraint column, in constraint-column order
        Needs: metadata visibility (db_datareader)
   --------------------------------------------------------------------------- */
PRINT '===== Q4 Foreign keys =====';
SELECT  s.name  AS TABLE_SCHEMA, t.name  AS TABLE_NAME, fk.name AS FK_NAME,
        pc.name AS COLUMN_NAME,
        rs.name AS REF_SCHEMA,   rt.name AS REF_TABLE,  rc.name AS REF_COLUMN,
        fk.delete_referential_action_desc AS ON_DELETE,
        fk.update_referential_action_desc AS ON_UPDATE
FROM    sys.foreign_keys fk
JOIN    sys.tables  t  ON t.object_id  = fk.parent_object_id
JOIN    sys.schemas s  ON s.schema_id  = t.schema_id
JOIN    sys.tables  rt ON rt.object_id = fk.referenced_object_id
JOIN    sys.schemas rs ON rs.schema_id = rt.schema_id
JOIN    sys.foreign_key_columns fkc ON fkc.constraint_object_id = fk.object_id
JOIN    sys.columns pc ON pc.object_id = fkc.parent_object_id     AND pc.column_id = fkc.parent_column_id
JOIN    sys.columns rc ON rc.object_id = fkc.referenced_object_id AND rc.column_id = fkc.referenced_column_id
WHERE   t.is_ms_shipped = 0
  AND   fk.is_ms_shipped = 0
  AND   s.name NOT IN ('sys', 'INFORMATION_SCHEMA', 'guest', 'db_owner', 'db_accessadmin', 'db_securityadmin', 'db_ddladmin', 'db_backupoperator', 'db_datareader', 'db_datawriter', 'db_denydatareader', 'db_denydatawriter')
ORDER BY s.name, t.name, fk.name, fkc.constraint_column_id;


/* ---------------------------------------------------------------------------
   Q5 — Rowstore indexes   [scanner.py _INDEXES_SQL]
        one row per index column; PK-backing indexes excluded
        Needs: metadata visibility (db_datareader)
   --------------------------------------------------------------------------- */
PRINT '===== Q5 Rowstore indexes =====';
SELECT  s.name AS TABLE_SCHEMA, t.name AS TABLE_NAME, i.name AS INDEX_NAME,
        i.is_unique AS IS_UNIQUE, i.filter_definition AS FILTER_DEFINITION,
        c.name AS COLUMN_NAME, ic.key_ordinal AS KEY_ORDINAL,
        ic.is_descending_key AS IS_DESCENDING, ic.is_included_column AS IS_INCLUDED
FROM    sys.indexes i
JOIN    sys.tables  t ON t.object_id = i.object_id
JOIN    sys.schemas s ON s.schema_id = t.schema_id
JOIN    sys.index_columns ic ON ic.object_id = i.object_id AND ic.index_id = i.index_id
JOIN    sys.columns c ON c.object_id = ic.object_id AND c.column_id = ic.column_id
WHERE   i.type IN (1, 2)
  AND   i.is_primary_key = 0
  AND   i.is_hypothetical = 0
  AND   i.is_disabled = 0
  AND   t.is_ms_shipped = 0
  AND   s.name NOT IN ('sys', 'INFORMATION_SCHEMA', 'guest', 'db_owner', 'db_accessadmin', 'db_securityadmin', 'db_ddladmin', 'db_backupoperator', 'db_datareader', 'db_datawriter', 'db_denydatareader', 'db_denydatawriter')
ORDER BY s.name, t.name, i.name, ic.is_included_column, ic.key_ordinal, ic.index_column_id;


/* ---------------------------------------------------------------------------
   Q6 — Column DEFAULT constraints   [scanner.py _DEFAULTS_SQL]
        raw T-SQL default expressions
        Needs: metadata visibility (db_datareader)
   --------------------------------------------------------------------------- */
PRINT '===== Q6 Column DEFAULT constraints =====';
SELECT  s.name AS TABLE_SCHEMA, t.name AS TABLE_NAME,
        c.name AS COLUMN_NAME, dc.definition AS DEFINITION
FROM    sys.default_constraints dc
JOIN    sys.tables  t ON t.object_id = dc.parent_object_id
JOIN    sys.schemas s ON s.schema_id = t.schema_id
JOIN    sys.columns c ON c.object_id = dc.parent_object_id AND c.column_id = dc.parent_column_id
WHERE   t.is_ms_shipped = 0
  AND   s.name NOT IN ('sys', 'INFORMATION_SCHEMA', 'guest', 'db_owner', 'db_accessadmin', 'db_securityadmin', 'db_ddladmin', 'db_backupoperator', 'db_datareader', 'db_datawriter', 'db_denydatareader', 'db_denydatawriter')
ORDER BY s.name, t.name, c.name;


/* ---------------------------------------------------------------------------
   Q7 — CHECK constraints   [scanner.py _CHECKS_SQL]
        enabled CHECK predicates, raw T-SQL
        Needs: metadata visibility (db_datareader)
   --------------------------------------------------------------------------- */
PRINT '===== Q7 CHECK constraints =====';
SELECT  s.name AS TABLE_SCHEMA, t.name AS TABLE_NAME,
        cc.name AS CHECK_NAME, cc.definition AS DEFINITION
FROM    sys.check_constraints cc
JOIN    sys.tables  t ON t.object_id = cc.parent_object_id
JOIN    sys.schemas s ON s.schema_id = t.schema_id
WHERE   cc.is_disabled = 0
  AND   t.is_ms_shipped = 0
  AND   s.name NOT IN ('sys', 'INFORMATION_SCHEMA', 'guest', 'db_owner', 'db_accessadmin', 'db_securityadmin', 'db_ddladmin', 'db_backupoperator', 'db_datareader', 'db_datawriter', 'db_denydatareader', 'db_denydatawriter')
ORDER BY s.name, t.name, cc.name;


/* ---------------------------------------------------------------------------
   Q8 — IDENTITY columns   [scanner.py _IDENTITY_SQL]
        at most one per table
        Needs: metadata visibility (db_datareader)
   --------------------------------------------------------------------------- */
PRINT '===== Q8 IDENTITY columns =====';
SELECT  s.name AS TABLE_SCHEMA, t.name AS TABLE_NAME, ic.name AS COLUMN_NAME
FROM    sys.identity_columns ic
JOIN    sys.tables  t ON t.object_id = ic.object_id
JOIN    sys.schemas s ON s.schema_id = t.schema_id
WHERE   t.is_ms_shipped = 0
  AND   s.name NOT IN ('sys', 'INFORMATION_SCHEMA', 'guest', 'db_owner', 'db_accessadmin', 'db_securityadmin', 'db_ddladmin', 'db_backupoperator', 'db_datareader', 'db_datawriter', 'db_denydatareader', 'db_denydatawriter');


/* ---------------------------------------------------------------------------
   Q9 — Programmable object definitions   [scanner.py _MODULES_SQL]
        procedures, views, functions, triggers — the T-SQL to translate
        Needs: VIEW DEFINITION  <-- without it DEFINITION is NULL or rows vanish, silently
   --------------------------------------------------------------------------- */
PRINT '===== Q9 Programmable object definitions =====';
SELECT  s.name AS SCHEMA_NAME, o.name AS OBJECT_NAME, o.type_desc AS OBJECT_TYPE,
        m.definition AS DEFINITION
FROM    sys.sql_modules m
JOIN    sys.objects o ON o.object_id = m.object_id
JOIN    sys.schemas s ON s.schema_id = o.schema_id
WHERE   o.type IN ('P', 'V', 'FN', 'IF', 'TF', 'TR')
  AND   o.is_ms_shipped = 0
  AND   s.name NOT IN ('sys', 'INFORMATION_SCHEMA', 'guest', 'db_owner', 'db_accessadmin', 'db_securityadmin', 'db_ddladmin', 'db_backupoperator', 'db_datareader', 'db_datawriter', 'db_denydatareader', 'db_denydatawriter');


/* ---------------------------------------------------------------------------
   Q10 — permission summary. Not issued by the app; run it to see, in one
         place, what the current login actually holds. Expect 1 for both
         has_* columns. DATABASE::  scopes the check to the current database,
         which is where both permissions live.
   --------------------------------------------------------------------------- */
PRINT '===== Q10 effective permissions =====';
SELECT  SUSER_SNAME()                                       AS login_name,
        USER_NAME()                                         AS db_user,
        DB_NAME()                                           AS database_name,
        HAS_PERMS_BY_NAME(DB_NAME(), 'DATABASE', 'VIEW DATABASE STATE')
                                                            AS has_view_database_state,
        HAS_PERMS_BY_NAME(DB_NAME(), 'DATABASE', 'VIEW DEFINITION')
                                                            AS has_view_definition,
        IS_ROLEMEMBER('db_datareader')                      AS in_db_datareader,
        IS_ROLEMEMBER('db_owner')                           AS in_db_owner;

/* The permissions as actually granted, whether directly or via a role. Useful
   when Q10 says 0 and you need to know what IS there. */
PRINT '===== Q10b granted permissions =====';
SELECT  dp.permission_name, dp.state_desc, dp.class_desc,
        USER_NAME(dp.grantee_principal_id) AS grantee
FROM    sys.database_permissions dp
WHERE   (dp.grantee_principal_id = DATABASE_PRINCIPAL_ID()
         OR dp.grantee_principal_id IN (
              SELECT role_principal_id FROM sys.database_role_members
              WHERE member_principal_id = DATABASE_PRINCIPAL_ID()))
  AND   dp.permission_name IN ('VIEW DEFINITION', 'VIEW DATABASE STATE', 'SELECT', 'CONNECT')
ORDER BY dp.permission_name;

/* Row counts per query, to spot the silent-omission case: a 0 here on a
   database that clearly has the objects means metadata visibility, not an
   empty database. */
PRINT '===== Q11 row counts per catalog view =====';
SELECT 'INFORMATION_SCHEMA.COLUMNS'  AS catalog_view, COUNT(*) AS visible_rows FROM INFORMATION_SCHEMA.COLUMNS
UNION ALL SELECT 'sys.tables',              COUNT(*) FROM sys.tables              WHERE is_ms_shipped = 0
UNION ALL SELECT 'sys.sql_modules',         COUNT(*) FROM sys.sql_modules
UNION ALL SELECT 'sys.foreign_keys',        COUNT(*) FROM sys.foreign_keys
UNION ALL SELECT 'sys.indexes',             COUNT(*) FROM sys.indexes
UNION ALL SELECT 'sys.default_constraints', COUNT(*) FROM sys.default_constraints
UNION ALL SELECT 'sys.check_constraints',   COUNT(*) FROM sys.check_constraints
UNION ALL SELECT 'sys.identity_columns',    COUNT(*) FROM sys.identity_columns;
