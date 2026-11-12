"use client";

import { useMutation, useQuery } from "@tanstack/react-query";
import { useEffect, useId, useRef, useState, type FormEvent } from "react";

import { Button } from "@/components/ui/button";
import { Alert, EmptyState, ErrorState, LoadingRegion, Skeleton } from "@/components/ui/feedback";
import { errorMessage } from "@/lib/api";
import { getDownload, startDownload } from "@/lib/documents";
import {
  isRetryable,
  listSearchableDocuments,
  MAX_QUERY_LENGTH,
  searchErrorMessage,
  searchKeys,
  searchWorkspace,
  similarityPercent,
  sourceLabel,
  validateQuery,
  type SearchInput,
  type SearchResult,
} from "@/lib/search";
import type { Workspace } from "@/lib/workspaces";

const ALL_DOCUMENTS = "all";
// Longer passages are folded so one result cannot push the others off the screen.
const LONG_EXCERPT_CHARACTERS = 600;

const CONTROL =
  "h-10 rounded-lg border border-zinc-200 bg-white px-3 text-sm text-zinc-900 placeholder:text-zinc-400 " +
  "focus-visible:outline-2 focus-visible:outline-offset-1 focus-visible:outline-zinc-900 " +
  "disabled:cursor-not-allowed disabled:bg-zinc-50";

function isAbort(error: unknown): boolean {
  return error instanceof DOMException && error.name === "AbortError";
}

function ResultCard({ result, position }: { result: SearchResult; position: number }) {
  const [expanded, setExpanded] = useState(false);
  const excerptId = useId();
  const download = useMutation({
    mutationFn: () => getDownload(result.document_id),
    onSuccess: (link) => startDownload(link.url),
  });

  const source = sourceLabel(result.source);
  const percent = similarityPercent(result.similarity);
  const long = result.content.length > LONG_EXCERPT_CHARACTERS;
  const shown =
    long && !expanded ? `${result.content.slice(0, LONG_EXCERPT_CHARACTERS).trimEnd()}…` : result.content;

  return (
    <li className="rounded-xl border border-zinc-200 bg-white p-4">
      <div className="flex flex-wrap items-start justify-between gap-x-4 gap-y-2">
        <div className="min-w-0">
          <h3 className="text-sm font-semibold break-words text-zinc-900">
            <span className="sr-only">Result {position}: </span>
            {result.document_name}
          </h3>
          {source && <p className="mt-0.5 text-xs text-zinc-500">{source}</p>}
        </div>
        <span
          className="shrink-0 rounded-full border border-zinc-200 bg-zinc-50 px-2 py-0.5 text-xs font-medium whitespace-nowrap text-zinc-700"
          title="How close in meaning this passage is to your search"
        >
          {percent}% match
        </span>
      </div>

      {/* Document text is shown as plain text. It is never rendered as HTML. */}
      <p id={excerptId} className="mt-3 text-sm leading-6 break-words whitespace-pre-line text-zinc-700">
        {shown}
      </p>

      {download.isError && (
        <div className="mt-3">
          <Alert>{errorMessage(download.error)}</Alert>
        </div>
      )}

      <div className="mt-3 flex flex-wrap gap-2">
        {long && (
          <Button
            variant="ghost"
            size="sm"
            aria-expanded={expanded}
            aria-controls={excerptId}
            onClick={() => setExpanded(!expanded)}
          >
            {expanded ? "Show less" : "Show more"}
          </Button>
        )}
        <Button
          variant="ghost"
          size="sm"
          loading={download.isPending}
          aria-label={`Download ${result.document_name}`}
          onClick={() => download.mutate()}
        >
          {download.isPending ? "Preparing…" : "Download source"}
        </Button>
      </div>
    </li>
  );
}

/** Semantic search over one workspace's documents. */
export function WorkspaceSearch({ workspace }: { workspace: Workspace }) {
  const inputId = useId();
  const filterId = useId();
  const [queryError, setQueryError] = useState<string | null>(null);
  const [documentFilter, setDocumentFilter] = useState(ALL_DOCUMENTS);
  const controller = useRef<AbortController | null>(null);

  const documents = useQuery({
    queryKey: searchKeys.searchable(workspace.id),
    queryFn: ({ signal }) => listSearchableDocuments(workspace.id, signal),
  });

  const search = useMutation({
    mutationFn: (input: SearchInput) => {
      // A newer search replaces one still in flight, so a slow answer never overwrites it.
      controller.current?.abort();
      controller.current = new AbortController();
      return searchWorkspace(workspace.id, input, controller.current.signal);
    },
  });

  // Leaving the page (or switching workspace) cancels a search that is still running.
  useEffect(() => () => controller.current?.abort(), []);

  function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const raw = String(new FormData(event.currentTarget).get("query") ?? "");
    const problem = validateQuery(raw);
    setQueryError(problem);
    if (problem) return;
    search.mutate({
      query: raw.trim(),
      documentIds: documentFilter === ALL_DOCUMENTS ? [] : [documentFilter],
    });
  }

  const failed = search.isError && !isAbort(search.error);
  const results = search.data?.results;
  const filteredName = documents.data?.find((document) => document.id === documentFilter)?.filename;
  const nothingToSearch = documents.isSuccess && documents.data.length === 0;

  return (
    <section aria-labelledby="search-heading" className="rounded-xl border border-zinc-200 bg-white p-5">
      <h2 id="search-heading" className="text-base font-semibold text-zinc-900">
        Search
      </h2>
      <p className="mt-1 text-sm text-zinc-600">
        Find passages in this workspace&apos;s documents by meaning, not just exact words.
      </p>

      <form onSubmit={handleSubmit} noValidate role="search" className="mt-4 flex flex-col gap-3">
        <div className="flex flex-wrap items-start gap-2">
          <div className="min-w-0 flex-1 basis-64">
            <label htmlFor={inputId} className="sr-only">
              Search this workspace&apos;s documents
            </label>
            <input
              id={inputId}
              name="query"
              type="search"
              placeholder="Ask a question, for example: What is the refund policy?"
              maxLength={MAX_QUERY_LENGTH}
              autoComplete="off"
              aria-invalid={queryError ? true : undefined}
              aria-describedby={queryError ? `${inputId}-error` : undefined}
              className={`${CONTROL} w-full ${queryError ? "border-red-500" : ""}`}
            />
          </div>
          <Button type="submit" loading={search.isPending}>
            {search.isPending ? "Searching…" : "Search"}
          </Button>
        </div>

        {queryError && (
          <p id={`${inputId}-error`} role="alert" className="text-sm text-red-700">
            {queryError}
          </p>
        )}

        <div className="flex flex-wrap items-center gap-2">
          <label htmlFor={filterId} className="text-sm text-zinc-600">
            Search in
          </label>
          <select
            id={filterId}
            value={documentFilter}
            onChange={(event) => setDocumentFilter(event.target.value)}
            disabled={!documents.data?.length}
            className={`${CONTROL} h-8 max-w-full px-2`}
          >
            <option value={ALL_DOCUMENTS}>All documents</option>
            {documents.data?.map((document) => (
              <option key={document.id} value={document.id}>
                {document.filename}
              </option>
            ))}
          </select>
        </div>
      </form>

      <div className="mt-5" aria-live="polite">
        {search.isPending ? (
          <LoadingRegion label="Searching documents">
            <div className="flex flex-col gap-3">
              {[0, 1, 2].map((index) => (
                <Skeleton key={index} className="h-28 rounded-xl" />
              ))}
            </div>
          </LoadingRegion>
        ) : failed ? (
          <ErrorState
            title="Search failed"
            description={searchErrorMessage(search.error)}
            onRetry={
              isRetryable(search.error) && search.variables
                ? () => search.mutate(search.variables as SearchInput)
                : undefined
            }
          />
        ) : results === undefined ? (
          nothingToSearch ? (
            <EmptyState
              title="Nothing to search yet"
              description="Upload a document below. Once it has finished processing, its text can be searched here."
            />
          ) : null
        ) : results.length === 0 ? (
          <EmptyState
            title="No matching passages"
            description={
              nothingToSearch
                ? "This workspace has no processed documents yet. Upload one below, then search again."
                : filteredName
                  ? `Nothing in “${filteredName}” matches “${search.data?.query}”. Try other words, or search all documents.`
                  : `Nothing in this workspace matches “${search.data?.query}”. Try other words or a fuller question.`
            }
          />
        ) : (
          <>
            <p className="mb-3 text-sm text-zinc-600">
              {results.length === 1 ? "1 passage" : `${results.length} passages`} for “{search.data?.query}”
              {filteredName ? ` in “${filteredName}”` : ""}, most relevant first.
            </p>
            <ol className="flex flex-col gap-3">
              {results.map((result, index) => (
                <ResultCard key={result.chunk_id} result={result} position={index + 1} />
              ))}
            </ol>
          </>
        )}
      </div>
    </section>
  );
}
