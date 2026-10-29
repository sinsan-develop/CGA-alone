/** S04의 판정 helper. 실제 모듈/Function/Talk 실행은 기존 simulator를 사용한다. */
export type SecondaryOutcomeStatus = "answered" | "unrecognized" | "pending" | "error";
export type SecondaryOutcomeMapping = { variableName: string; answeredValue: string; unrecognizedValue: string };
export type SecondaryModuleConfig = { module_id: string; outcome?: SecondaryOutcomeMapping };

export function secondaryOutcomeMapping(config: unknown): SecondaryOutcomeMapping | null {
  if (!config || typeof config !== "object") return null;
  const mapping = (config as Record<string, unknown>).outcome;
  if (!mapping || typeof mapping !== "object") return null;
  const row = mapping as Record<string, unknown>;
  if (!["variableName", "answeredValue", "unrecognizedValue"].every(key => typeof row[key] === "string" && (row[key] as string).trim())) return null;
  if (row.answeredValue === row.unrecognizedValue) return null;
  return { variableName: (row.variableName as string).trim(), answeredValue: row.answeredValue as string, unrecognizedValue: row.unrecognizedValue as string };
}

export function evaluateSecondaryOutcome<T extends Record<string, unknown>>(
  config: unknown,
  variables: T,
  flags: { waiting: boolean; executionError: boolean },
  resolve: (variables: T, name: string) => unknown = (source, name) => source[name] ?? source[name.replace(/^\$+/, "")] ?? source["$" + name.replace(/^\$+/, "")],
): { status: SecondaryOutcomeStatus } {
  const mapping = secondaryOutcomeMapping(config);
  if (!mapping || flags.executionError) return { status: "error" };
  if (flags.waiting) return { status: "pending" };
  const value = resolve(variables, mapping.variableName);
  if (value == null || !["string", "number", "boolean"].includes(typeof value)) return { status: "error" };
  return { status: String(value) === mapping.answeredValue ? "answered" : String(value) === mapping.unrecognizedValue ? "unrecognized" : "error" };
}

export function prepareSecondaryVariables<T extends Record<string, string>>(
  variables: T,
  config: unknown,
  normalize: (name: string) => string = name => name.replace(/^\$+/, ""),
): T {
  const result = { ...variables };
  const mapping = secondaryOutcomeMapping(config);
  if (!mapping) return result;
  const name = normalize(mapping.variableName);
  const aliases = new Set([mapping.variableName, name, "$" + name]);
  if (name.includes(".response.")) {
    const plain = name.replace(".response.", ".");
    aliases.add(plain); aliases.add("$" + plain);
  } else {
    const [root, ...path] = name.split(".");
    if (path.length) { const response = root + ".response." + path.join("."); aliases.add(response); aliases.add("$" + response); }
  }
  for (const alias of aliases) delete result[alias];
  return result;
}
