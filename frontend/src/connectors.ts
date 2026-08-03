// Connector catalog (Airbyte-style). Only Azure SQL is enabled in this release;
// the rest are shown as "Coming soon" so the roadmap is visible in the UI.

export interface Connector {
  id: string;
  name: string;
  category: string;
  description: string;
  abbr: string;     // shown in the colored tile when no logo asset is bundled
  color: string;    // brand color for the tile
  enabled: boolean;
  logo?: string;    // explicit logo asset path (overrides /logos/<id>.svg)
  // Connection-form hints (only needed for enabled sources).
  hostPlaceholder?: string;
  usernamePlaceholder?: string;
  defaultPort?: number;
  connectionNote?: string;
}

export const SOURCE_CONNECTORS: Connector[] = [
  {
    id: "azure-sql",
    name: "Azure SQL Database",
    category: "Microsoft",
    description: "Fully-managed SQL Server on Azure.",
    abbr: "AZ",
    color: "#0078D4",
    enabled: true,
    hostPlaceholder: "myserver.database.windows.net",
    usernamePlaceholder: "user@servername",
    defaultPort: 1433,
    connectionNote: "Azure SQL requires the login in user@servername form and an open firewall rule for this app's egress IP.",
  },
  {
    id: "sql-server",
    name: "SQL Server",
    category: "Microsoft",
    description: "On-prem / IaaS Microsoft SQL Server (2016+).",
    abbr: "MS",
    color: "#CC2927",
    enabled: true,
    hostPlaceholder: "sqlserver.internal.corp  (or 10.0.0.5)",
    usernamePlaceholder: "sa",
    defaultPort: 1433,
    connectionNote: "Use a SQL Server authentication login. The host must be reachable from the app (VNet / private link).",
  },
  { id: "oracle", name: "Oracle Database", category: "Database", description: "Oracle 11g–23c via JDBC.", abbr: "OR", color: "#F80000", enabled: false },
  { id: "postgres", name: "PostgreSQL", category: "Database", description: "Self-managed or cloud Postgres.", abbr: "PG", color: "#336791", enabled: false },
  { id: "mysql", name: "MySQL", category: "Database", description: "MySQL 5.7 / 8.x.", abbr: "My", color: "#4479A1", enabled: false },
  { id: "mariadb", name: "MariaDB", category: "Database", description: "MariaDB server.", abbr: "Ma", color: "#003545", enabled: false },
  { id: "db2", name: "IBM Db2", category: "Database", description: "IBM Db2 LUW / z/OS.", abbr: "DB", color: "#052FAD", enabled: false },
  { id: "mongodb", name: "MongoDB", category: "NoSQL", description: "Document store migration to relational.", abbr: "Mo", color: "#47A248", enabled: false },
  { id: "snowflake", name: "Snowflake", category: "Warehouse", description: "Snowflake data warehouse.", abbr: "SF", color: "#29B5E8", enabled: false },
  { id: "redshift", name: "Amazon Redshift", category: "Warehouse", description: "AWS Redshift cluster.", abbr: "RS", color: "#8C4FFF", enabled: false },
  { id: "bigquery", name: "Google BigQuery", category: "Warehouse", description: "GCP BigQuery datasets.", abbr: "BQ", color: "#4285F4", enabled: false },
  { id: "cockroachdb", name: "CockroachDB", category: "Database", description: "Distributed SQL.", abbr: "CR", color: "#6933FF", enabled: false },
];

// Single, fixed destination for this accelerator.
export const LAKEBASE_DESTINATION: Connector = {
  id: "lakebase",
  name: "Databricks Lakebase",
  category: "Databricks",
  description: "Managed serverless Postgres in the Lakehouse.",
  abbr: "LB",
  color: "#FF3621",
  enabled: true,
  logo: "/lakebase-icon.png",
};
