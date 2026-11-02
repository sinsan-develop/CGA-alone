import { type VersionDocument } from "@/lib/version-document";

export type VersionApiHttpMethod = "GET" | "POST";
export type VersionApiParameterLocation = "query" | "path" | "header" | "body";
export type VersionApiDataType = "string" | "number" | "boolean" | "object" | "array";

export type VersionApiParameter = {
  id: string;
  name: string;
  location: VersionApiParameterLocation;
  dataType: VersionApiDataType;
  defaultValue: string;
  required: boolean;
  visible: boolean;
  description: string;
};

export type VersionApiOutputParameter = {
  id: string;
  name: string;
  path: string;
  dataType: VersionApiDataType;
  description: string;
};

export type VersionApiMethod = {
  id: string;
  name: string;
  httpMethod: VersionApiHttpMethod;
  methodUrl: string;
  description: string;
  loggingEnabled: boolean;
  proxyEnabled: boolean;
  transferMode: "sync";
  parameters: VersionApiParameter[];
  outputParameters: VersionApiOutputParameter[];
  outputSample: string;
};

export type VersionApiAsset = {
  id: string;
  type: "api";
  apiKey: string;
  name: string;
  baseUrl: string;
  description: string;
  category: string;
  methods: VersionApiMethod[];
  updatedAt: string;
  updatedBy: string;
  usageCount?: number;
};

export type ApiExportPayload = {
  asset_format_version: number;
  exported_at: string;
  apis: VersionApiAsset[];
};

// HTTP 개발 주소에서도 API 자산 식별자를 암호학적 난수로 생성한다.
export function createApiAssetId(): string {
  if (typeof globalThis.crypto.randomUUID === "function") {
    return globalThis.crypto.randomUUID();
  }
  const bytes = globalThis.crypto.getRandomValues(new Uint8Array(16));
  bytes[6] = (bytes[6] & 0x0f) | 0x40;
  bytes[8] = (bytes[8] & 0x3f) | 0x80;
  const hex = Array.from(bytes, (value) => value.toString(16).padStart(2, "0")).join("");
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
}

function asRecord(value: unknown): Record<string, unknown> | null {
  return value && typeof value === "object" && !Array.isArray(value) ? (value as Record<string, unknown>) : null;
}

function readString(source: Record<string, unknown>, keys: string[], fallback = "") {
  for (const key of keys) {
    const value = source[key];
    if (typeof value === "string") {
      return value;
    }
    if (typeof value === "number" || typeof value === "boolean") {
      return String(value);
    }
  }
  return fallback;
}

function readBoolean(source: Record<string, unknown>, keys: string[], fallback = false) {
  for (const key of keys) {
    const value = source[key];
    if (typeof value === "boolean") {
      return value;
    }
  }
  return fallback;
}

function readNumber(source: Record<string, unknown>, keys: string[], fallback = 0) {
  for (const key of keys) {
    const value = source[key];
    if (typeof value === "number" && Number.isFinite(value)) {
      return value;
    }
  }
  return fallback;
}

function normalizeHttpMethod(value: unknown): VersionApiHttpMethod {
  return typeof value === "string" && value.trim().toUpperCase() === "POST" ? "POST" : "GET";
}

function normalizeParameterLocation(value: unknown): VersionApiParameterLocation {
  if (value === "path" || value === "header" || value === "body") {
    return value;
  }
  return "query";
}

function normalizeDataType(value: unknown): VersionApiDataType {
  if (value === "number" || value === "boolean" || value === "object" || value === "array") {
    return value;
  }
  return "string";
}

export function createEmptyApiParameter(location: VersionApiParameterLocation = "query"): VersionApiParameter {
  return {
    id: createApiAssetId(),
    name: "",
    location,
    dataType: "string",
    defaultValue: "",
    required: false,
    visible: true,
    description: "",
  };
}

export function createEmptyApiMethod(): VersionApiMethod {
  return {
    id: createApiAssetId(),
    name: "Method 1",
    httpMethod: "GET",
    methodUrl: "",
    description: "",
    loggingEnabled: false,
    proxyEnabled: true,
    transferMode: "sync",
    parameters: [],
    outputParameters: [],
    outputSample: "",
  };
}

export function createEmptyApiAsset(): VersionApiAsset {
  const id = createApiAssetId();
  return {
    id,
    type: "api",
    apiKey: createApiAssetId(),
    name: "",
    baseUrl: "",
    description: "",
    category: "API",
    methods: [createEmptyApiMethod()],
    updatedAt: new Date().toISOString(),
    updatedBy: "",
  };
}

export function normalizeApiParameter(value: unknown): VersionApiParameter | null {
  const source = asRecord(value);
  if (!source) {
    return null;
  }

  return {
    id: readString(source, ["id"], createApiAssetId()),
    name: readString(source, ["name", "parameterName"]).trim(),
    location: normalizeParameterLocation(source.location ?? source.parameterType),
    dataType: normalizeDataType(source.dataType),
    defaultValue: readString(source, ["defaultValue", "default"]),
    required: readBoolean(source, ["required", "requiredYn"], false),
    visible: readBoolean(source, ["visible", "visibleYn"], true),
    description: readString(source, ["description"]),
  };
}

export function normalizeApiOutputParameter(value: unknown): VersionApiOutputParameter | null {
  const source = asRecord(value);
  if (!source) {
    return null;
  }

  const name = readString(source, ["name", "parameterName"]).trim();
  const path = readString(source, ["path", "jsonPath"], name).trim();
  if (!name && !path) {
    return null;
  }

  return {
    id: readString(source, ["id"], createApiAssetId()),
    name: name || path,
    path,
    dataType: normalizeDataType(source.dataType),
    description: readString(source, ["description"]),
  };
}

export function normalizeApiMethod(value: unknown, index = 0): VersionApiMethod | null {
  const source = asRecord(value);
  if (!source) {
    return null;
  }

  const parameters = Array.isArray(source.parameters)
    ? source.parameters.map((item) => normalizeApiParameter(item)).filter((item): item is VersionApiParameter => item !== null)
    : [];
  const outputParameters = Array.isArray(source.outputParameters)
    ? source.outputParameters
        .map((item) => normalizeApiOutputParameter(item))
        .filter((item): item is VersionApiOutputParameter => item !== null)
    : [];

  return {
    id: readString(source, ["id"], createApiAssetId()),
    name: readString(source, ["name", "methodName"], `Method ${index + 1}`).trim() || `Method ${index + 1}`,
    httpMethod: normalizeHttpMethod(source.httpMethod ?? source.method),
    methodUrl: readString(source, ["methodUrl", "path", "url"]).trim(),
    description: readString(source, ["description"]),
    loggingEnabled: readBoolean(source, ["loggingEnabled", "logging"], false),
    proxyEnabled: readBoolean(source, ["proxyEnabled", "proxy"], true),
    transferMode: "sync",
    parameters,
    outputParameters,
    outputSample: "",
  };
}

export function normalizeApiAsset(value: unknown): VersionApiAsset | null {
  const source = asRecord(value);
  if (!source) {
    return null;
  }

  const name = readString(source, ["name", "apiName"]).trim();
  const baseUrl = readString(source, ["baseUrl", "destinationBaseUrl", "destinationUrl"]).trim();
  const methods = Array.isArray(source.methods)
    ? source.methods.map((item, index) => normalizeApiMethod(item, index)).filter((item): item is VersionApiMethod => item !== null)
    : [];

  if (!name && !baseUrl) {
    return null;
  }

  return {
    id: readString(source, ["id"], createApiAssetId()),
    type: "api",
    apiKey: readString(source, ["apiKey", "key"], createApiAssetId()).trim(),
    name,
    baseUrl,
    description: readString(source, ["description"]),
    category: "API",
    methods: methods.length > 0 ? methods : [createEmptyApiMethod()],
    updatedAt: readString(source, ["updatedAt", "updated_at"], new Date().toISOString()),
    updatedBy: readString(source, ["updatedBy", "updated_by"]),
    usageCount: readNumber(source, ["usageCount", "usage_count"], 0),
  };
}

export function normalizeApiAssets(value: unknown): VersionApiAsset[] {
  if (!Array.isArray(value)) {
    return [];
  }

  return value.map((item) => normalizeApiAsset(item)).filter((item): item is VersionApiAsset => item !== null);
}

function stripApiOutputSamples(apis: VersionApiAsset[]) {
  return apis.map(({ usageCount: _usageCount, ...api }) => ({
    ...api,
    methods: api.methods.map((method) => ({
      ...method,
      outputSample: "",
    })),
  }));
}

export function withUpdatedApiAssets(document: VersionDocument, apis: VersionApiAsset[]): VersionDocument {
  return {
    ...document,
    apis: stripApiOutputSamples(apis),
  };
}

export function buildApiExportPayload(apis: VersionApiAsset[]): ApiExportPayload {
  return {
    asset_format_version: 1,
    exported_at: new Date().toISOString(),
    apis: stripApiOutputSamples(apis),
  };
}

export function parseApiImportPayload(value: unknown): VersionApiAsset[] {
  const source = asRecord(value);
  const rawApis = Array.isArray(value) ? value : Array.isArray(source?.apis) ? source.apis : [];
  return normalizeApiAssets(rawApis);
}

export function joinApiUrl(baseUrl: string, methodUrl: string, variables: Record<string, string>) {
  const base = baseUrl.trim().replace(/\/+$/, "");
  let path = methodUrl.trim();

  for (const [key, value] of Object.entries(variables)) {
    path = path.replace(new RegExp(`\\{${key}\\}`, "g"), encodeURIComponent(value));
  }

  if (/^https?:\/\//i.test(path)) {
    return path;
  }

  return `${base}/${path.replace(/^\/+/, "")}`;
}
