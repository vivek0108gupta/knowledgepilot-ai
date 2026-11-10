"use client";

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useId, useRef, useState, type ChangeEvent, type DragEvent } from "react";

import { Button } from "@/components/ui/button";
import { Alert } from "@/components/ui/feedback";
import { Modal } from "@/components/ui/modal";
import { errorMessage } from "@/lib/api";
import { cn } from "@/lib/cn";
import { ACCEPT_ATTRIBUTE, documentKeys, formatBytes, uploadDocument, validateFile } from "@/lib/documents";

interface UploadDialogProps {
  workspaceId: string;
  open: boolean;
  onOpenChange: (open: boolean) => void;
}

function isAbort(error: unknown): boolean {
  return error instanceof DOMException && error.name === "AbortError";
}

export function UploadDialog({ workspaceId, open, onOpenChange }: UploadDialogProps) {
  const queryClient = useQueryClient();
  const inputId = useId();
  const progressId = useId();
  const [file, setFile] = useState<File | null>(null);
  const [fileError, setFileError] = useState<string | null>(null);
  const [dragging, setDragging] = useState(false);
  const [progress, setProgress] = useState(0);
  const controller = useRef<AbortController | null>(null);

  const upload = useMutation({
    mutationFn: (chosen: File) => {
      controller.current = new AbortController();
      return uploadDocument(workspaceId, chosen, {
        onProgress: setProgress,
        signal: controller.current.signal,
      });
    },
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: documentKeys.workspace(workspaceId) });
      close();
    },
  });

  const uploading = upload.isPending;
  const uploadError = upload.isError && !isAbort(upload.error) ? errorMessage(upload.error) : null;
  const percent = Math.round(progress * 100);

  function reset() {
    setFile(null);
    setFileError(null);
    setDragging(false);
    setProgress(0);
    upload.reset();
  }

  function close() {
    reset();
    onOpenChange(false);
  }

  function handleOpenChange(next: boolean) {
    if (next) {
      onOpenChange(true);
      return;
    }
    // Closing mid-upload cancels it rather than leaving it running out of sight.
    controller.current?.abort();
    close();
  }

  function choose(chosen: File | undefined) {
    if (!chosen) return;
    upload.reset();
    setProgress(0);
    setFile(chosen);
    setFileError(validateFile(chosen));
  }

  function handleInputChange(event: ChangeEvent<HTMLInputElement>) {
    choose(event.target.files?.[0]);
    // Let the same file be picked again after it is removed.
    event.target.value = "";
  }

  function handleDrop(event: DragEvent<HTMLLabelElement>) {
    event.preventDefault();
    setDragging(false);
    if (uploading) return;
    const dropped = event.dataTransfer.files;
    if (dropped.length > 1) {
      setFile(null);
      setFileError("Upload one file at a time.");
      return;
    }
    choose(dropped[0]);
  }

  return (
    <Modal
      open={open}
      onOpenChange={handleOpenChange}
      title="Upload document"
      description="PDF, DOCX or TXT, up to 25 MB. Scanned PDFs are not supported."
    >
      <div className="flex flex-col gap-4">
        {uploadError && <Alert>{uploadError}</Alert>}

        <label
          htmlFor={inputId}
          onDragOver={(event) => {
            event.preventDefault();
            if (!uploading) setDragging(true);
          }}
          onDragLeave={() => setDragging(false)}
          onDrop={handleDrop}
          className={cn(
            "flex cursor-pointer flex-col items-center gap-1 rounded-xl border-2 border-dashed px-4 py-8 text-center transition-colors",
            "focus-within:outline-2 focus-within:outline-offset-2 focus-within:outline-zinc-900",
            dragging
              ? "border-zinc-900 bg-zinc-100"
              : "border-zinc-300 hover:border-zinc-400 hover:bg-zinc-50",
            uploading && "pointer-events-none opacity-60",
          )}
        >
          <span className="text-sm font-medium text-zinc-900">
            {dragging ? "Drop the file to select it" : "Drag a file here, or click to choose one"}
          </span>
          <span className="text-sm text-zinc-500">PDF, DOCX or TXT · up to 25 MB</span>
          <input
            id={inputId}
            type="file"
            accept={ACCEPT_ATTRIBUTE}
            onChange={handleInputChange}
            disabled={uploading}
            className="sr-only"
            aria-describedby={fileError ? `${inputId}-error` : undefined}
            aria-invalid={fileError ? true : undefined}
          />
        </label>

        {file && (
          <div className="flex items-center justify-between gap-3 rounded-lg border border-zinc-200 px-3 py-2">
            <div className="flex min-w-0 flex-col">
              <span className="truncate text-sm font-medium text-zinc-900">{file.name}</span>
              <span className="text-xs text-zinc-500">{formatBytes(file.size)}</span>
            </div>
            {!uploading && (
              <Button variant="ghost" size="sm" onClick={reset} aria-label={`Remove ${file.name}`}>
                Remove
              </Button>
            )}
          </div>
        )}

        {fileError && (
          <p id={`${inputId}-error`} role="alert" className="text-sm text-red-700">
            {fileError}
          </p>
        )}

        {uploading && (
          <div className="flex flex-col gap-1.5">
            <div className="flex justify-between text-sm text-zinc-600">
              <span id={progressId}>Uploading…</span>
              <span>{percent}%</span>
            </div>
            <div
              role="progressbar"
              aria-labelledby={progressId}
              aria-valuemin={0}
              aria-valuemax={100}
              aria-valuenow={percent}
              className="h-2 overflow-hidden rounded-full bg-zinc-100"
            >
              <div
                className="h-full rounded-full bg-zinc-900 transition-[width]"
                style={{ width: `${percent}%` }}
              />
            </div>
          </div>
        )}

        <div className="flex justify-end gap-2">
          <Button variant="secondary" onClick={() => handleOpenChange(false)}>
            Cancel
          </Button>
          <Button
            loading={uploading}
            disabled={!file || fileError !== null}
            onClick={() => file && upload.mutate(file)}
          >
            {uploading ? "Uploading…" : "Upload"}
          </Button>
        </div>
      </div>
    </Modal>
  );
}
