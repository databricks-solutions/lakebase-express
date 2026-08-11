/* Lakebase Express — GRANTs for the source (Azure SQL / SQL Server) login.
   Replace <login>. Verify with scripts/assessment_scan_queries.sql.
   Idempotent; each database needs its own run. */


-- 0. Create the user if needed — pick the one matching your auth.
-- CREATE USER [<login>] FOR LOGIN [<login>];              -- SQL auth
-- CREATE USER [user@tenant.com] FROM EXTERNAL PROVIDER;   -- Entra ID
-- CREATE USER [<login>] WITH PASSWORD = '<password>';     -- contained user


-- 1. Assessment. Reads catalog views only, never table data — so
--    db_datareader is not required to assess a database.
GRANT CONNECT TO [<login>];

-- sys.sql_modules.definition: the T-SQL to translate. Fails SILENTLY without
-- this — rows vanish or DEFINITION is NULL, no error.
GRANT VIEW DEFINITION TO [<login>];

-- sys.dm_db_partition_stats: row counts. Raises, and being the 2nd query in
-- scan_tables it aborts the scan with a 502. Usual cause of an outright failure.
GRANT VIEW DATABASE STATE TO [<login>];

-- The remaining queries need only metadata visibility, which VIEW DEFINITION
-- already grants database-wide.


-- 2. Later phases only (data migration, validation, query parity) — these read
--    actual data. Nothing writes to the source: no db_datawriter/ddladmin/owner.
ALTER ROLE db_datareader ADD MEMBER [<login>];


-- 3. Verify. Expect 1 in both has_* columns.
SELECT  SUSER_SNAME() AS login_name,
        USER_NAME()   AS db_user,
        DB_NAME()     AS database_name,
        HAS_PERMS_BY_NAME(DB_NAME(), 'DATABASE', 'VIEW DEFINITION')     AS has_view_definition,
        HAS_PERMS_BY_NAME(DB_NAME(), 'DATABASE', 'VIEW DATABASE STATE') AS has_view_database_state,
        IS_ROLEMEMBER('db_datareader')                                  AS in_db_datareader;

-- 0 rows here on a database that has the objects means visibility is still missing.
SELECT 'sys.sql_modules'             AS catalog_view, COUNT(*) AS visible_rows FROM sys.sql_modules
UNION ALL SELECT 'sys.dm_db_partition_stats', COUNT(*) FROM sys.dm_db_partition_stats
UNION ALL SELECT 'INFORMATION_SCHEMA.COLUMNS', COUNT(*) FROM INFORMATION_SCHEMA.COLUMNS;
