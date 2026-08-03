export type IdentifierCase = "lowercase" | "preserve";

/** Frontend mirror of backend/schema_migration/naming.py for target previews. */
export function mapSchema(
  sourceSchema: string,
  defaultSchema: string,
  identifierCase: IdentifierCase = "lowercase",
): string {
  const rawDefault = (defaultSchema || "public").trim() || "public";
  const mappedDefault = identifierCase === "preserve" ? rawDefault : rawDefault.toLowerCase();
  const source = (sourceSchema || "").trim();
  if (!source || source.toLowerCase() === "dbo") return mappedDefault;
  return identifierCase === "preserve" ? source : source.toLowerCase();
}

export function mapObject(name: string, identifierCase: IdentifierCase = "lowercase"): string {
  const value = name || "";
  return identifierCase === "preserve" ? value : value.toLowerCase();
}
