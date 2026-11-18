"use client";

import { keepPreviousData, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import { useAuth } from "@/components/auth/auth-provider";
import { Button } from "@/components/ui/button";
import { Alert, EmptyState, ErrorState, LoadingRegion, Skeleton } from "@/components/ui/feedback";
import { Modal } from "@/components/ui/modal";
import { errorMessage } from "@/lib/api";
import { cn } from "@/lib/cn";
import {
  canManageDocument,
  deleteDocument,
  documentKeys,
  documentStatusSchema,
  fileTypeLabel,
  formatBytes,
  getDownload,
  isInProgress,
  listDocuments,
  PAGE_SIZE,
  retryDocument,
  startDownload,
  STATUS_LABELS,
  type DocumentItem,
  type DocumentQuery,
  type DocumentStatus,
} from "@/lib/documents";
import type { Workspace } from "@/lib/workspaces";

import { UploadDialog } from "./upload-dialog";

const POLL_INTERVAL_MS = 2500;

const dateTime = new Intl.DateTimeFormat(undefined, { dateStyle: "medium", timeStyle: "short" });

const STATUS_STYLES: Record<DocumentStatus, string> = {
  uploaded: "border-zinc-200 bg-zinc-50 text-zinc-700",
  processing: "border-blue-200 bg-blue-50 text-blue-800",
  ready: "border-emerald-200 bg-emerald-50 text-emerald-800",
  failed: "border-red-200 bg-red-50 text-red-800",
};

const STATUS_DOTS: Record<DocumentStatus, string> = {
  uploaded: "bg-zinc-400",
  processing: "animate-pulse bg-blue-500",
  ready: "bg-emerald-500",
  failed: "bg-red-500",
};

/** Status as text plus colour, so it never depends on colour alone. */
export function StatusBadge({ status }: { status: DocumentStatus }) {
  return (
    <span
      className={cn(
        "inline-flex items-center gap-1.5 rounded-full border px-2 py-0.5 text-xs font-medium whitespace-nowrap",
        STATUS_STYLES[status],
      )}
    >
      <span aria-hidden="true" className={cn("size-1.5 rounded-full", STATUS_DOTS[status])} />
      {STATUS_LABELS[status]}
    </span>
  );
}

function useDownload() {
  return useMutation({
    mutationFn: (document: DocumentItem) => getDownload(document.id),
    onSuccess: (download) => startDownload(download.url),
  });
}

interface DetailsProps {
  document: DocumentItem;
  workspace: Workspace;
  onClose: () => void;
  onDelete: (document: DocumentItem) => void;
}

function DocumentDetails({ document, workspace, onClose, onDelete }: DetailsProps) {
  const queryClient = useQueryClient();
  const { user } = useAuth();
  const download = useDownload();
  const manageable = canManageDocument(workspace.role, document, user?.id);

  const retry = useMutation({
    mutationFn: () => retryDocument(document.id),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: documentKeys.workspace(workspace.id) }),
  });

  const rows: Array<[string, React.ReactNode]> = [
    ["Type", fileTypeLabel(document.mime_type)],
    ["Size", formatBytes(document.size_bytes)],
    ["Status", <StatusBadge key="status" status={document.status} />],
    ...(document.page_count !== null ? [["Pages", document.page_count] as [string, React.ReactNode]] : []),
    ["Uploaded", dateTime.format(new Date(document.created_at))],
    [
      "Uploaded by",
      document.uploaded_by === user?.id ? (
        "You"
      ) : (
        <code key="uploader" className="font-mono text-xs break-all">
          {document.uploaded_by}
        </code>
      ),
    ],
  ];

  return (
    <Modal open onOpenChange={(open) => !open && onClose()} title={document.filename}>
      <div className="flex flex-col gap-4">
        {download.isError && <Alert>{errorMessage(download.error)}</Alert>}
        {retry.isError && <Alert>{errorMessage(retry.error)}</Alert>}

        {document.status === "failed" && (
          <Alert>
            <p className="font-medium">This document could not be processed.</p>
            <p>{document.error ?? "No further details are available."}</p>
          </Alert>
        )}
        {isInProgress(document.status) && (
          <Alert tone="info">
            This document is being prepared. It will be searchable once processing finishes.
          </Alert>
        )}

        <dl className="grid grid-cols-[auto_1fr] gap-x-6 gap-y-2 text-sm">
          {rows.map(([label, value]) => (
            <div key={label} className="contents">
              <dt className="text-zinc-500">{label}</dt>
              <dd className="min-w-0 text-zinc-900">{value}</dd>
            </div>
          ))}
        </dl>

        <div className="flex flex-wrap justify-end gap-2">
          {manageable && (
            <Button variant="ghost" onClick={() => onDelete(document)}>
              Delete
            </Button>
          )}
          {manageable && document.status === "failed" && (
            <Button variant="secondary" loading={retry.isPending} onClick={() => retry.mutate()}>
              {retry.isPending ? "Retrying…" : "Try again"}
            </Button>
          )}
          <Button loading={download.isPending} onClick={() => download.mutate(document)}>
            {download.isPending ? "Preparing…" : "Download"}
          </Button>
        </div>
      </div>
    </Modal>
  );
}

interface DeleteProps {
  document: DocumentItem;
  workspaceId: string;
  onClose: () => void;
  onDeleted: () => void;
}

function DeleteDocumentDialog({ document, workspaceId, onClose, onDeleted }: DeleteProps) {
  const queryClient = useQueryClient();
  const remove = useMutation({
    mutationFn: () => deleteDocument(document.id),
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: documentKeys.workspace(workspaceId) });
      onDeleted();
    },
  });

  return (
    <Modal
      open
      onOpenChange={(open) => !open && !remove.isPending && onClose()}
      title={`Delete “${document.filename}”?`}
      description="The file is removed for everyone in this workspace. It cannot be undone."
    >
      {remove.isError && <Alert>{errorMessage(remove.error)}</Alert>}
      <div className="flex justify-end gap-2">
        <Button variant="secondary" onClick={onClose} disabled={remove.isPending}>
          Cancel
        </Button>
        <Button variant="danger" loading={remove.isPending} onClick={() => remove.mutate()}>
          {remove.isPending ? "Deleting…" : "Delete document"}
        </Button>
      </div>
    </Modal>
  );
}

const FILTERS: Array<DocumentStatus | "all"> = ["all", ...documentStatusSchema.options];

/** A workspace's documents: list, upload, details, download and delete. */
export function Documents({ workspace }: { workspace: Workspace }) {
  const { user } = useAuth();
  const [query, setQuery] = useState<DocumentQuery>({ status: "all", page: 0 });
  const [uploadOpen, setUploadOpen] = useState(false);
  const [detailsId, setDetailsId] = useState<string | null>(null);
  const [deleting, setDeleting] = useState<DocumentItem | null>(null);
  const download = useDownload();

  const documents = useQuery({
    queryKey: documentKeys.list(workspace.id, query),
    queryFn: ({ signal }) => listDocuments(workspace.id, query, signal),
    // Keep the current rows on screen while the next page or filter loads.
    placeholderData: keepPreviousData,
    // Poll only while something is still being processed.
    refetchInterval: (current) =>
      current.state.data?.items.some((item) => isInProgress(item.status)) ? POLL_INTERVAL_MS : false,
  });

  const page = documents.data;
  const details = page?.items.find((item) => item.id === detailsId) ?? null;
  const pageCount = page ? Math.max(1, Math.ceil(page.total / PAGE_SIZE)) : 1;
  const filtered = query.status !== "all";

  return (
    <section aria-labelledby="documents-heading" className="rounded-xl border border-zinc-200 bg-white p-5">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h2 id="documents-heading" className="text-base font-semibold text-zinc-900">
            Documents
          </h2>
          <p className="mt-1 text-sm text-zinc-600">PDF, DOCX and TXT files, up to 25 MB each.</p>
        </div>
        <Button onClick={() => setUploadOpen(true)}>Upload document</Button>
      </div>

      <div className="mt-4 flex flex-wrap items-center gap-2">
        <label htmlFor="document-status-filter" className="text-sm text-zinc-600">
          Status
        </label>
        <select
          id="document-status-filter"
          value={query.status}
          onChange={(event) => setQuery({ status: event.target.value as DocumentQuery["status"], page: 0 })}
          className="h-8 rounded-lg border border-zinc-200 bg-white px-2 text-sm text-zinc-900 focus-visible:outline-2 focus-visible:outline-offset-1 focus-visible:outline-zinc-900"
        >
          {FILTERS.map((status) => (
            <option key={status} value={status}>
              {status === "all" ? "All" : STATUS_LABELS[status]}
            </option>
          ))}
        </select>
      </div>

      <div className="mt-4">
        {download.isError && (
          <div className="mb-3">
            <Alert>{errorMessage(download.error)}</Alert>
          </div>
        )}

        {documents.isPending ? (
          <LoadingRegion label="Loading documents">
            <div className="flex flex-col gap-2">
              {[0, 1, 2].map((index) => (
                <Skeleton key={index} className="h-12" />
              ))}
            </div>
          </LoadingRegion>
        ) : documents.isError ? (
          <ErrorState
            title="Could not load documents"
            description={errorMessage(documents.error)}
            onRetry={() => void documents.refetch()}
          />
        ) : page && page.items.length === 0 ? (
          filtered ? (
            <EmptyState
              title="No documents with this status"
              description="Choose another status to see the rest of this workspace's documents."
              action={
                <Button variant="secondary" onClick={() => setQuery({ status: "all", page: 0 })}>
                  Show all
                </Button>
              }
            />
          ) : (
            <EmptyState
              title="No documents yet"
              description="Upload a PDF, Word document or text file to start building this workspace's knowledge."
              action={<Button onClick={() => setUploadOpen(true)}>Upload document</Button>}
            />
          )
        ) : (
          page && (
            <>
              <div className="overflow-x-auto rounded-lg border border-zinc-200">
                <table className="w-full text-left text-sm">
                  <caption className="sr-only">Documents in this workspace</caption>
                  <thead className="border-b border-zinc-200 bg-zinc-50 text-xs text-zinc-500">
                    <tr>
                      <th scope="col" className="px-4 py-2 font-medium">
                        Name
                      </th>
                      <th scope="col" className="hidden px-4 py-2 font-medium sm:table-cell">
                        Type
                      </th>
                      <th scope="col" className="hidden px-4 py-2 font-medium sm:table-cell">
                        Size
                      </th>
                      <th scope="col" className="px-4 py-2 font-medium">
                        Status
                      </th>
                      <th scope="col" className="hidden px-4 py-2 font-medium md:table-cell">
                        Uploaded
                      </th>
                      <th scope="col" className="px-4 py-2 text-right font-medium">
                        <span className="sr-only">Actions</span>
                      </th>
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-zinc-200">
                    {page.items.map((item) => (
                      <tr key={item.id}>
                        <th scope="row" className="max-w-0 px-4 py-3 font-medium sm:max-w-xs">
                          <button
                            type="button"
                            onClick={() => setDetailsId(item.id)}
                            className="block max-w-full truncate rounded text-left text-zinc-900 underline-offset-4 hover:underline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-zinc-900"
                          >
                            {item.filename}
                          </button>
                          <span className="block text-xs font-normal text-zinc-500 sm:hidden">
                            {fileTypeLabel(item.mime_type)} · {formatBytes(item.size_bytes)}
                          </span>
                        </th>
                        <td className="hidden px-4 py-3 text-zinc-600 sm:table-cell">
                          {fileTypeLabel(item.mime_type)}
                        </td>
                        <td className="hidden px-4 py-3 whitespace-nowrap text-zinc-600 sm:table-cell">
                          {formatBytes(item.size_bytes)}
                        </td>
                        <td className="px-4 py-3">
                          <StatusBadge status={item.status} />
                        </td>
                        <td className="hidden px-4 py-3 whitespace-nowrap text-zinc-600 md:table-cell">
                          {dateTime.format(new Date(item.created_at))}
                        </td>
                        <td className="px-4 py-3">
                          <div className="flex justify-end gap-1">
                            <Button
                              variant="ghost"
                              size="sm"
                              aria-label={`Download ${item.filename}`}
                              loading={download.isPending && download.variables?.id === item.id}
                              onClick={() => download.mutate(item)}
                            >
                              Download
                            </Button>
                            {canManageDocument(workspace.role, item, user?.id) && (
                              <Button
                                variant="ghost"
                                size="sm"
                                aria-label={`Delete ${item.filename}`}
                                onClick={() => setDeleting(item)}
                              >
                                Delete
                              </Button>
                            )}
                          </div>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>

              <div className="mt-3 flex flex-wrap items-center justify-between gap-3 text-sm text-zinc-600">
                <p aria-live="polite">
                  {page.total === 1 ? "1 document" : `${page.total} documents`}
                  {pageCount > 1 && ` · Page ${query.page + 1} of ${pageCount}`}
                </p>
                {pageCount > 1 && (
                  <div className="flex gap-2">
                    <Button
                      variant="secondary"
                      size="sm"
                      disabled={query.page === 0}
                      onClick={() => setQuery({ ...query, page: query.page - 1 })}
                    >
                      Previous
                    </Button>
                    <Button
                      variant="secondary"
                      size="sm"
                      disabled={query.page + 1 >= pageCount}
                      onClick={() => setQuery({ ...query, page: query.page + 1 })}
                    >
                      Next
                    </Button>
                  </div>
                )}
              </div>
            </>
          )
        )}
      </div>

      <UploadDialog workspaceId={workspace.id} open={uploadOpen} onOpenChange={setUploadOpen} />

      {details && !deleting && (
        <DocumentDetails
          document={details}
          workspace={workspace}
          onClose={() => setDetailsId(null)}
          onDelete={setDeleting}
        />
      )}

      {deleting && (
        <DeleteDocumentDialog
          document={deleting}
          workspaceId={workspace.id}
          onClose={() => setDeleting(null)}
          onDeleted={() => {
            setDeleting(null);
            setDetailsId(null);
            // Deleting the only row on a later page would otherwise leave an empty page.
            if (page && page.items.length === 1 && query.page > 0) {
              setQuery({ ...query, page: query.page - 1 });
            }
          }}
        />
      )}
    </section>
  );
}
