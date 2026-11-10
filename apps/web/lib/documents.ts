import { z } from "zod";

import { apiFetch, apiUpload, type UploadRequest } from "./api";
import type { Role } from "./workspaces";

export const documentStatusSchema = z.enum(["uploaded", "processing", "ready", "failed"]);
export type DocumentStatus = z.infer<typeof documentStatusSchema>;

export const STATUS_LABELS: Record<DocumentStatus, string> = {
  uploaded: "Queued",
  processing: "Processing",
  ready: "Ready",
  failed: "Failed",
};

export const documentSchema = z.object({
  id: z.uuid(),
  workspace_id: z.uuid(),
  uploaded_by: z.uuid(),
  filename: z.string(),
  mime_type: z.string(),
  size_bytes: z.number(),
  status: documentStatusSchema,
  page_count: z.number().nullable(),
  error: z.string().nullable(),
  created_at: z.string(),
  updated_at: z.string(),
});
export type DocumentItem = z.infer<typeof documentSchema>;

export const documentPageSchema = z.object({
  items: z.array(documentSchema),
  total: z.number(),
  limit: z.number(),
  offset: z.number(),
});
export type DocumentPage = z.infer<typeof documentPageSchema>;

const downloadSchema = z.object({ url: z.url(), expires_in: z.number(), filename: z.string() });

/** 25 MB. The API enforces the same limit; this only saves a doomed upload. */
export const MAX_FILE_BYTES = 25 * 1024 * 1024;

export const ACCEPTED_EXTENSIONS = ["pdf", "docx", "txt"] as const;

/** For the file input's `accept`, so the picker offers only supported files. */
export const ACCEPT_ATTRIBUTE = [
  ".pdf",
  ".docx",
  ".txt",
  "application/pdf",
  "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
  "text/plain",
].join(",");

export const PAGE_SIZE = 10;

const TYPE_LABELS: Record<string, string> = {
  "application/pdf": "PDF",
  "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "Word",
  "text/plain": "Text",
};

export const fileTypeLabel = (mimeType: string) => TYPE_LABELS[mimeType] ?? "File";

export function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

/**
 * Why a chosen file cannot be uploaded, or null if it can.
 * A convenience only: the API checks the file's real contents and is the authority.
 */
export function validateFile(file: Pick<File, "name" | "size">): string | null {
  const extension = file.name.includes(".") ? file.name.split(".").pop()!.toLowerCase() : "";
  if (!(ACCEPTED_EXTENSIONS as readonly string[]).includes(extension)) {
    return "Only PDF, DOCX and TXT files can be uploaded.";
  }
  if (file.size === 0) return "This file is empty.";
  if (file.size > MAX_FILE_BYTES) {
    return `This file is ${formatBytes(file.size)}. Files can be up to 25 MB.`;
  }
  return null;
}

/** True while the server is still working on a document, so the list should keep polling. */
export const isInProgress = (status: DocumentStatus) => status === "uploaded" || status === "processing";

/** Delete and retry: the uploader, or an admin or owner. The API enforces the same rule. */
export function canManageDocument(role: Role, document: DocumentItem, userId: string | undefined): boolean {
  return role === "owner" || role === "admin" || document.uploaded_by === userId;
}

export interface DocumentQuery {
  status: DocumentStatus | "all";
  page: number;
}

export const documentKeys = {
  workspace: (workspaceId: string) => ["workspaces", workspaceId, "documents"] as const,
  list: (workspaceId: string, query: DocumentQuery) =>
    ["workspaces", workspaceId, "documents", query.status, query.page] as const,
};

export function listDocuments(workspaceId: string, query: DocumentQuery, signal?: AbortSignal) {
  const params = new URLSearchParams({
    limit: String(PAGE_SIZE),
    offset: String(query.page * PAGE_SIZE),
  });
  if (query.status !== "all") params.set("status", query.status);
  return apiFetch(`/workspaces/${workspaceId}/documents?${params}`, documentPageSchema, { signal });
}

export function uploadDocument(workspaceId: string, file: File, request?: UploadRequest) {
  const form = new FormData();
  form.append("file", file);
  return apiUpload(`/workspaces/${workspaceId}/documents`, documentSchema, form, request);
}

export const getDownload = (documentId: string) =>
  apiFetch(`/documents/${documentId}/download`, downloadSchema);

export const retryDocument = (documentId: string) =>
  apiFetch(`/documents/${documentId}/retry`, documentSchema, { method: "POST" });

export const deleteDocument = (documentId: string) =>
  apiFetch(`/documents/${documentId}`, z.void(), { method: "DELETE" });

/**
 * Starts a browser download from a signed link. The link asks storage to send the file as an
 * attachment, so the current page stays where it is.
 */
export function startDownload(url: string): void {
  window.location.assign(url);
}
