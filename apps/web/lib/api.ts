/** Base URL of the FastAPI backend. Inlined at build time, so it must be set before `next build`. */
export const API_URL = (process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000").replace(/\/$/, "");

export const API_V1 = `${API_URL}/api/v1`;

/** The shape every API error arrives in; see `app/core/errors.py` in the backend. */
interface ErrorEnvelope {
  error: { code: string; message: string; request_id?: string };
}

function isErrorEnvelope(value: unknown): value is ErrorEnvelope {
  if (typeof value !== "object" || value === null || !("error" in value)) return false;
  const { error } = value;
  return (
    typeof error === "object" &&
    error !== null &&
    "code" in error &&
    typeof error.code === "string" &&
    "message" in error &&
    typeof error.message === "string"
  );
}

/** A failed API call. `status` is 0 when the request never got a response. */
export class ApiError extends Error {
  readonly status: number;
  readonly code: string;
  /** Quote this when reporting a problem; it matches a line in the API logs. */
  readonly requestId: string | undefined;

  constructor(status: number, code: string, message: string, requestId?: string) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.code = code;
    this.requestId = requestId;
  }
}

/**
 * Calls a `/api/v1` endpoint and returns its JSON body.
 * Always rejects with an `ApiError`, except when the caller aborts the request.
 */
export async function apiFetch<T>(path: string, init?: RequestInit): Promise<T> {
  let response: Response;
  try {
    response = await fetch(`${API_V1}${path}`, {
      ...init,
      headers: { Accept: "application/json", ...init?.headers },
    });
  } catch (cause) {
    if (init?.signal?.aborted) throw cause;
    throw new ApiError(0, "network_error", "Could not reach the server.");
  }

  const body: unknown = await response.json().catch(() => null);

  if (!response.ok) {
    const requestId = response.headers.get("X-Request-ID") ?? undefined;
    if (isErrorEnvelope(body)) {
      throw new ApiError(response.status, body.error.code, body.error.message, body.error.request_id ?? requestId);
    }
    throw new ApiError(response.status, "unexpected_error", `Request failed with status ${response.status}.`, requestId);
  }

  return body as T;
}

export interface Health {
  status: string;
}

export interface Readiness extends Health {
  database: string;
  pgvector: string;
}

export const getHealth = (init?: RequestInit) => apiFetch<Health>("/health", init);

export const getReadiness = (init?: RequestInit) => apiFetch<Readiness>("/health/ready", init);
