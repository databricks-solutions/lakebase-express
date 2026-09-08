// Connector catalog (Airbyte-style). Azure SQL and SQL Server are enabled in this
// release; the remaining relational sources are shown as "Coming soon" so the
// roadmap is visible in the UI.

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
    usernamePlaceholder: "your-username",
    defaultPort: 1433,
    connectionNote: "The Azure SQL firewall must allow this app's egress IP (or \"Allow Azure services\").",
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
