"""Post-migration validation: compare the source database with the Lakebase
target (object coverage, row counts, table structure) and help remediate any
inconsistency — deterministically where possible, with the Foundation Model
otherwise. Independent of the migration steps; it re-scans both sides live."""
