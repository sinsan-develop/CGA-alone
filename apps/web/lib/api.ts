import { redirectToLogin } from "@/lib/auth";

const DEFAULT_SERVER_API_BASE_URL = "http://localhost:8320";

export const API_BASE_URL =
  typeof window === "undefined"
    ? process.env.CGA_INTERNAL_API_BASE_URL ?? DEFAULT_SERVER_API_BASE_URL
    : "";
const API_SLOW_REQUEST_THRESHOLD_MS = Number(process.env.NEXT_PUBLIC_API_SLOW_REQUEST_THRESHOLD_MS ?? "1000");

type ApiEnvelope<T> = {
  data: T;
  meta?: {
    request_id?: string;
  };
};

export function formatApiErrorDetail(detail: unknown) {
  if (typeof detail === "string") {
    return detail;
  }
  if (Array.isArray(detail)) {
    return detail
      .map((item) => {
        if (!item || typeof item !== "object" || Array.isArray(item)) {
          return String(item);
        }
        const record = item as Record<string, unknown>;
        const loc = Array.isArray(record.loc) ? record.loc.join(".") : "";
        const message = typeof record.msg === "string" ? record.msg : JSON.stringify(record);
        return loc ? `${loc}: ${message}` : message;
      })
      .join(" / ");
  }
  if (detail && typeof detail === "object") {
    const record = detail as Record<string, unknown>;
    const message = typeof record.message === "string" ? record.message.trim() : "";
    const validation = record.scenario_validation;
    if (validation && typeof validation === "object" && !Array.isArray(validation)) {
      const validationRecord = validation as Record<string, unknown>;
      const rawItems = Array.isArray(validationRecord.save_blocking_items)
        ? validationRecord.save_blocking_items
        : Array.isArray(validationRecord.items)
          ? validationRecord.items
          : [];
      const itemMessages = rawItems
        .filter((item): item is Record<string, unknown> => Boolean(item) && typeof item === "object" && !Array.isArray(item))
        .map((item) => {
          const dialogName = typeof item.dialog_name === "string" ? item.dialog_name.trim() : "";
          const nodeTitle = typeof item.node_title === "string" ? item.node_title.trim() : "";
          const itemMessage = typeof item.message === "string" ? item.message.trim() : "";
          const location = [dialogName, nodeTitle].filter(Boolean).join(" / ");
          return location && itemMessage ? `${location}: ${itemMessage}` : itemMessage;
        })
        .filter(Boolean)
        .slice(0, 3);
      return [message, ...itemMessages].filter(Boolean).join(" ");
    }
    if (message) {
      return message;
    }
    return JSON.stringify(detail);
  }
  return "";
}

function buildHeaders(headersInit?: HeadersInit, token?: string, body?: BodyInit | null) {
  const headers = new Headers(headersInit);
  if (body && !(body instanceof FormData)) {
    headers.set("Content-Type", "application/json");
  }
  if (token) {
    headers.set("Authorization", `Bearer ${token}`);
  }
  return headers;
}

function isAbsoluteUrl(value: string) {
  return /^https?:\/\//i.test(value) || value.startsWith("//");
}

export function resolveApiPublicUrl(path: string) {
  if (!path || isAbsoluteUrl(path)) {
    return path;
  }
  const normalizedPath = path.startsWith("/") ? path : `/${path}`;
  if (typeof window !== "undefined") {
    return normalizedPath;
  }
  return `${API_BASE_URL}${normalizedPath}`;
}

/** Public API static assets are not covered by the same-origin /api/v1 proxy. */
export function resolveApiAssetPublicUrl(path: string) {
  if (!path || isAbsoluteUrl(path)) {
    return path;
  }
  const normalizedPath = path.startsWith("/") ? path : `/${path}`;
  if (typeof window !== "undefined") {
    return normalizedPath;
  }
  return `${API_BASE_URL.replace(/\/+$/, "")}${normalizedPath}`;
}

export async function apiRequest<T>(
  path: string,
  init: RequestInit = {},
  token?: string,
): Promise<T> {
  let response: Response;
  const startedAt = performance.now();
  const requestUrl = resolveApiPublicUrl(path);
  try {
    response = await fetch(requestUrl, {
      ...init,
      headers: buildHeaders(init.headers, token, init.body ?? null),
      cache: "no-store",
    });
  } catch (error) {
    throw new Error(
      error instanceof TypeError
        ? "API 서버에 연결할 수 없습니다. 서버 실행 상태와 API 주소를 확인해주세요."
        : "API 요청 중 오류가 발생했습니다.",
    );
  }
  const elapsedMs = Math.round((performance.now() - startedAt) * 100) / 100;
  const serverElapsedMs = response.headers.get("X-Response-Time-Ms");
  if (API_SLOW_REQUEST_THRESHOLD_MS > 0 && elapsedMs >= API_SLOW_REQUEST_THRESHOLD_MS) {
    console.warn("[CGA API slow]", {
      path,
      method: init.method ?? "GET",
      status: response.status,
      elapsed_ms: elapsedMs,
      server_elapsed_ms: serverElapsedMs,
      request_id: response.headers.get("X-Request-Id"),
    });
  }

  const payload = (await response.json().catch(() => null)) as ApiEnvelope<T> | { detail?: unknown } | null;

  if (response.status === 401) {
    redirectToLogin();
    throw new Error("로그인이 필요합니다.");
  }

  if (!response.ok) {
    const message =
      payload && "detail" in payload && payload.detail
        ? formatApiErrorDetail(payload.detail)
        : "요청 처리 중 오류가 발생했습니다.";
    throw new Error(message);
  }

  if (!payload || !("data" in payload)) {
    throw new Error("응답 형식이 올바르지 않습니다.");
  }

  return payload.data;
}

export async function apiSameOriginRequest<T>(
  path: string,
  init: RequestInit = {},
  token?: string,
): Promise<T> {
  let response: Response;
  const startedAt = performance.now();
  try {
    response = await fetch(path, {
      ...init,
      headers: buildHeaders(init.headers, token, init.body ?? null),
      cache: "no-store",
    });
  } catch (error) {
    throw new Error(
      error instanceof TypeError
        ? "API 서버에 연결할 수 없습니다. 서버 실행 상태와 API 주소를 확인해주세요."
        : "API 요청 중 오류가 발생했습니다.",
    );
  }
  const elapsedMs = Math.round((performance.now() - startedAt) * 100) / 100;
  const serverElapsedMs = response.headers.get("X-Response-Time-Ms");
  if (API_SLOW_REQUEST_THRESHOLD_MS > 0 && elapsedMs >= API_SLOW_REQUEST_THRESHOLD_MS) {
    console.warn("[CGA API slow]", {
      path,
      method: init.method ?? "GET",
      status: response.status,
      elapsed_ms: elapsedMs,
      server_elapsed_ms: serverElapsedMs,
      request_id: response.headers.get("X-Request-Id"),
    });
  }

  const payload = (await response.json().catch(() => null)) as ApiEnvelope<T> | { detail?: unknown } | null;

  if (response.status === 401) {
    redirectToLogin();
    throw Object.assign(new Error("로그인이 필요합니다."), { status: response.status });
  }

  if (!response.ok) {
    const message =
      payload && "detail" in payload && payload.detail
        ? formatApiErrorDetail(payload.detail)
        : "요청 처리 중 오류가 발생했습니다.";
    throw Object.assign(new Error(message), { status: response.status });
  }

  if (!payload || !("data" in payload)) {
    throw new Error("응답 형식이 올바르지 않습니다.");
  }

  return payload.data;
}
