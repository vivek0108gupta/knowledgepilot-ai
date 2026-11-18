import { z } from "zod";

import { ApiError, apiFetch } from "./api";
import { documentPageSchema } from "./documents";

export const MAX_QUERY_LENGTH = 1000;
export const DEFAULT_TOP_K = 5;

const sourceSchema = z.object({
  page_number: z.number().nullable(),
  paragraph_index: z.number().nullable(),
  line_start: z.number().nullable(),
  line_end: z.number().nullable(),
});
export type SearchSource = z.infer<typeof sourceSchema>;

export const searchResultSchema = z.object({
  chunk_id: z.uuid(),
  document_id: z.uuid(),
  document_name: z.string(),
  content: z.string(),
  similarity: z.number(),
  source: sourceSchema,
});
export type SearchResult = z.infer<typeof searchResultSchema>;

export const searchResponseSchema = z.object({
  query: z.string(),
  results: z.array(searchResultSchema),
});
export type SearchResponse = z.infer<typeof searchResponseSchema>;

export interface SearchInput {
  query: string;
  /** Restrict the search to these documents. Empty means the whole workspace. */
  documentIds?: string[];
  topK?: number;
}

/**
 * Searches one workspace. The workspace comes from the page the user is on and the API
 * checks their membership of it; there is no way to ask for results across workspaces.
 */
export function searchWorkspace(workspaceId: string, input: SearchInput, signal?: AbortSignal) {
  return apiFetch(`/workspaces/${workspaceId}/search`, searchResponseSchema, {
    method: "POST",
    body: {
      query: input.query,
      top_k: input.topK ?? DEFAULT_TOP_K,
      document_ids: input.documentIds ?? [],
    },
    signal,
  });
}

/** Documents that can be searched, for the optional filter. */
export async function listSearchableDocuments(workspaceId: string, signal?: AbortSignal) {
  const page = await apiFetch(
    `/workspaces/${workspaceId}/documents?status=ready&limit=100&offset=0`,
    documentPageSchema,
    { signal },
  );
  return page.items;
}

export const searchKeys = {
  searchable: (workspaceId: string) => ["workspaces", workspaceId, "documents", "searchable"] as const,
};

/** Why a query cannot be sent, or null if it can. The API applies the same limits. */
export function validateQuery(raw: string): string | null {
  const query = raw.trim();
  if (!query) return "Enter something to search for.";
  if (query.length > MAX_QUERY_LENGTH) return `Use ${MAX_QUERY_LENGTH.toLocaleString()} characters or fewer.`;
  return null;
}

/**
 * Where a passage sits in its document, in words. Only what the file format records is
 * shown: a Word document has no page numbers, so it is cited by paragraph.
 */
export function sourceLabel(source: SearchSource): string | null {
  if (source.page_number !== null) return `Page ${source.page_number}`;
  // Stored from zero; people count paragraphs from one.
  if (source.paragraph_index !== null) return `Paragraph ${source.paragraph_index + 1}`;
  if (source.line_start !== null) {
    return source.line_end !== null && source.line_end !== source.line_start
      ? `Lines ${source.line_start}–${source.line_end}`
      : `Line ${source.line_start}`;
  }
  return null;
}

/** Cosine similarity as a whole percentage between 0 and 100. */
export function similarityPercent(similarity: number): number {
  return Math.round(Math.min(1, Math.max(0, similarity)) * 100);
}

/** A message for a failed search that says what the person can do about it. */
export function searchErrorMessage(error: unknown): string {
  if (!(error instanceof ApiError)) return "Something went wrong. Please try again.";
  if (error.status === 0) return "Could not reach the server. Check your connection and try again.";
  if (error.status === 401) return "Your session has expired. Sign in again to search.";
  if (error.status === 403) return "You do not have permission to search this workspace.";
  if (error.status === 404) return "This workspace no longer exists, or you are no longer a member of it.";
  if (error.status === 422) return "That search could not be run. Shorten or rephrase it and try again.";
  if (error.status === 429) return "Too many searches in a short time. Wait a moment and try again.";
  if (error.code === "embeddings_not_configured") return "Search has not been set up on the server yet.";
  if (error.status >= 500) return "Search is temporarily unavailable. Try again in a moment.";
  return "The search could not be completed. Please try again.";
}

/** Whether trying the same search again could plausibly succeed. */
export function isRetryable(error: unknown): boolean {
  if (!(error instanceof ApiError)) return true;
  if (error.code === "embeddings_not_configured") return false;
  return error.status === 0 || error.status === 429 || error.status >= 500;
}
