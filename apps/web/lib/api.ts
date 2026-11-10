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

export interface ApiAuth {
  /** The current access token, or null when signed out. */
  getAccessToken: () => Promise<string | null>;
  /** Called when the API rejects the token, so the app can end the session. */
  onUnauthorized: () => void;
}

let apiAuth: ApiAuth | null = null;

/** Connects the client to the auth session. Set once by `AuthProvider`; pass null to disconnect. */
export function configureApiAuth(auth: ApiAuth | null): void {
  apiAuth = auth;
}

export interface ApiRequest {
  method?: "GET" | "POST" | "PATCH" | "DELETE";
  /** Sent as JSON. */
  body?: unknown;
  signal?: AbortSignal;
}

/**
 * Calls a `/api/v1` endpoint and returns its JSON body, checked against `schema`.
 * Sends the session's access token when there is one. The API works out who is calling from
 * that token alone, so this client never sends a user ID to identify the caller.
 * Always rejects with an `ApiError`, except when the caller aborts the request.
 */
export async function apiFetch<T>(path: string, schema: z.ZodType<T>, request: ApiRequest = {}): Promise<T> {
  const { method = "GET", body: payload, signal } = request;

  const headers: Record<string, string> = { Accept: "application/json" };
  if (payload !== undefined) headers["Content-Type"] = "application/json";
  const token = apiAuth ? await apiAuth.getAccessToken() : null;
  if (token) headers.Authorization = `Bearer ${token}`;

  let response: Response;
  try {
    response = await fetch(`${API_V1}${path}`, {
      method,
      headers,
      body: payload === undefined ? undefined : JSON.stringify(payload),
      signal,
    });
  } catch (cause) {
    if (signal?.aborted) throw cause;
    throw new ApiError(0, "network_error", "Could not reach the server.");
  }

  const body: unknown = response.status === 204 ? undefined : await response.json().catch(() => null);
  const requestId = response.headers.get("X-Request-ID") ?? undefined;

  if (!response.ok) {
    if (response.status === 401) apiAuth?.onUnauthorized();

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

/** A message safe to show a user for any thrown value. */
export function errorMessage(error: unknown): string {
  return error instanceof ApiError ? error.message : "Something went wrong. Please try again.";
}

export const healthSchema = z.object({ status: z.string() });
export type Health = z.infer<typeof healthSchema>;

export const readinessSchema = healthSchema.extend({ database: z.string(), pgvector: z.string() });
export type Readiness = z.infer<typeof readinessSchema>;

export const getHealth = (signal?: AbortSignal) => apiFetch("/health", healthSchema, { signal });

export const getReadiness = (signal?: AbortSignal) => apiFetch("/health/ready", readinessSchema, { signal });

export interface UploadRequest {
  /** Called with a number from 0 to 1 as the file is sent. */
  onProgress?: (fraction: number) => void;
  signal?: AbortSignal;
}

/**
 * Sends a form (with files) to a `/api/v1` endpoint and returns the JSON body, checked against
 * `schema`. Uses XMLHttpRequest because `fetch` cannot report upload progress.
 * Rejects exactly like `apiFetch`: with an `ApiError`, or the abort reason if the caller aborts.
 */
export async function apiUpload<T>(
  path: string,
  schema: z.ZodType<T>,
  form: FormData,
  { onProgress, signal }: UploadRequest = {},
): Promise<T> {
  const token = apiAuth ? await apiAuth.getAccessToken() : null;

  return new Promise<T>((resolve, reject) => {
    const aborted = () => new DOMException("The upload was cancelled.", "AbortError");
    if (signal?.aborted) {
      reject(aborted());
      return;
    }

    const xhr = new XMLHttpRequest();
    xhr.open("POST", `${API_V1}${path}`);
    xhr.setRequestHeader("Accept", "application/json");
    if (token) xhr.setRequestHeader("Authorization", `Bearer ${token}`);
    // No Content-Type: the browser sets it, including the multipart boundary.

    const abort = () => xhr.abort();
    signal?.addEventListener("abort", abort);
    const finish = () => signal?.removeEventListener("abort", abort);

    xhr.upload.onprogress = (event) => {
      if (event.lengthComputable && event.total > 0) onProgress?.(event.loaded / event.total);
    };
    xhr.onabort = () => {
      finish();
      reject(aborted());
    };
    xhr.onerror = () => {
      finish();
      reject(new ApiError(0, "network_error", "Could not reach the server."));
    };
    xhr.onload = () => {
      finish();
      const requestId = xhr.getResponseHeader("X-Request-ID") ?? undefined;
      let body: unknown = null;
      try {
        body = JSON.parse(xhr.responseText);
      } catch {
        // Not JSON (for example an error page from a proxy); handled below as unexpected.
      }

      if (xhr.status < 200 || xhr.status >= 300) {
        if (xhr.status === 401) apiAuth?.onUnauthorized();
        const envelope = errorEnvelopeSchema.safeParse(body);
        reject(
          envelope.success
            ? new ApiError(
                xhr.status,
                envelope.data.error.code,
                envelope.data.error.message,
                envelope.data.error.request_id ?? requestId,
              )
            : new ApiError(
                xhr.status,
                "unexpected_error",
                `Request failed with status ${xhr.status}.`,
                requestId,
              ),
        );
        return;
      }

      const parsed = schema.safeParse(body);
      if (!parsed.success) {
        reject(
          new ApiError(xhr.status, "invalid_response", "The server sent an unexpected response.", requestId),
        );
        return;
      }
      resolve(parsed.data);
    };

    xhr.send(form);
  });
}
