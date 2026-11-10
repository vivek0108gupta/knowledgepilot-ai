import { z } from "zod";

/** Base URL of the FastAPI backend. Inlined at build time, so it must be set before `next build`. */
export const API_URL = (process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000").replace(/\/$/, "");

export const API_V1 = `${API_URL}/api/v1`;

/** The shape every API error arrives in; see `app/core/errors.py` in the backend. */
const errorEnvelopeSchema = z.object({
  error: z.object({
    code: z.string(),
    message: z.string(),
    request_id: z.string().optional(),
  }),
});

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
 * Calls a `/api/v1` endpoint and returns its JSON body, checked against `schema`.
 * Always rejects with an `ApiError`, except when the caller aborts the request.
 */
export async function apiFetch<T>(path: string, schema: z.ZodType<T>, init?: RequestInit): Promise<T> {
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
  const requestId = response.headers.get("X-Request-ID") ?? undefined;

  if (!response.ok) {
    const envelope = errorEnvelopeSchema.safeParse(body);
    if (envelope.success) {
      const { code, message, request_id } = envelope.data.error;
      throw new ApiError(response.status, code, message, request_id ?? requestId);
    }
    throw new ApiError(
      response.status,
      "unexpected_error",
      `Request failed with status ${response.status}.`,
      requestId,
    );
  }

  const parsed = schema.safeParse(body);
  if (!parsed.success) {
    // The backend and this client disagree about the response shape: fail loudly, not downstream.
    throw new ApiError(
      response.status,
      "invalid_response",
      "The server sent an unexpected response.",
      requestId,
    );
  }
  return parsed.data;
}

export const healthSchema = z.object({ status: z.string() });
export type Health = z.infer<typeof healthSchema>;

export const readinessSchema = healthSchema.extend({ database: z.string(), pgvector: z.string() });
export type Readiness = z.infer<typeof readinessSchema>;

export const getHealth = (init?: RequestInit) => apiFetch("/health", healthSchema, init);

export const getReadiness = (init?: RequestInit) => apiFetch("/health/ready", readinessSchema, init);
