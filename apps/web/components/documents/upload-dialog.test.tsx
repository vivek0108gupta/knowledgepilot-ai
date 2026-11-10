import { fireEvent, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, expect, test, vi } from "vitest";

import { ApiError, type UploadRequest } from "@/lib/api";
import { renderWithQuery } from "@/test/render";

import { UploadDialog } from "./upload-dialog";

const WORKSPACE = "8a5c1f0e-0000-4000-8000-000000000000";

const mocks = vi.hoisted(() => ({ uploadDocument: vi.fn() }));

vi.mock("@/lib/documents", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/documents")>()),
  uploadDocument: mocks.uploadDocument,
}));

const pdf = (name = "report.pdf", size = 2048) =>
  new File([new Uint8Array(size)], name, { type: "application/pdf" });

function renderDialog() {
  const onOpenChange = vi.fn();
  const view = renderWithQuery(<UploadDialog workspaceId={WORKSPACE} open onOpenChange={onOpenChange} />);
  return { onOpenChange, ...view };
}

const fileInput = () => screen.getByLabelText(/Drag a file here/) as HTMLInputElement;
const uploadButton = () => screen.getByRole("button", { name: "Upload" });

/** `applyAccept: false` lets a test pick a file the picker's filter would have hidden. */
async function select(file: File) {
  await userEvent.setup({ applyAccept: false }).upload(fileInput(), file);
}

beforeEach(() => {
  mocks.uploadDocument.mockReset().mockResolvedValue({ id: "doc-1" });
});

test("is a labelled dialog with a keyboard-reachable file input and nothing to upload yet", async () => {
  renderDialog();

  expect(await screen.findByRole("dialog", { name: "Upload document" })).toBeInTheDocument();
  expect(fileInput()).toHaveAttribute("type", "file");
  expect(fileInput().accept).toContain(".pdf");
  expect(uploadButton()).toBeDisabled();
});

test("shows the chosen file's name and size, and enables upload", async () => {
  renderDialog();

  await select(pdf("Quarterly report.pdf", 1536));

  expect(screen.getByText("Quarterly report.pdf")).toBeInTheDocument();
  expect(screen.getByText("1.5 KB")).toBeInTheDocument();
  expect(uploadButton()).toBeEnabled();
});

test("uploads the chosen file to this workspace, refreshes the list and closes", async () => {
  const { onOpenChange, queryClient } = renderDialog();
  const invalidate = vi.spyOn(queryClient, "invalidateQueries");
  const file = pdf();

  await select(file);
  await userEvent.setup().click(uploadButton());

  await vi.waitFor(() => expect(onOpenChange).toHaveBeenCalledWith(false));
  expect(mocks.uploadDocument).toHaveBeenCalledTimes(1);
  expect(mocks.uploadDocument.mock.calls[0]!.slice(0, 2)).toEqual([WORKSPACE, file]);
  expect(invalidate).toHaveBeenCalledWith({ queryKey: ["workspaces", WORKSPACE, "documents"] });
});

test.each([
  [new File(["MZ"], "malware.exe", { type: "application/x-msdownload" }), "Only PDF, DOCX and TXT files"],
  [new File(["x"], "report.pdf.exe", { type: "application/pdf" }), "Only PDF, DOCX and TXT files"],
  [new File([], "empty.pdf", { type: "application/pdf" }), "This file is empty."],
])("refuses an unacceptable file before sending anything", async (file, message) => {
  renderDialog();

  await select(file);

  expect(screen.getByRole("alert")).toHaveTextContent(message);
  expect(fileInput()).toBeInvalid();
  expect(uploadButton()).toBeDisabled();
  expect(mocks.uploadDocument).not.toHaveBeenCalled();
});

test("refuses a file over 25 MB", async () => {
  renderDialog();
  const big = pdf("big.pdf", 1);
  Object.defineProperty(big, "size", { value: 25 * 1024 * 1024 + 1 });

  await select(big);

  expect(screen.getByRole("alert")).toHaveTextContent("Files can be up to 25 MB.");
  expect(uploadButton()).toBeDisabled();
});

test("a rejected file can be replaced by a good one", async () => {
  renderDialog();

  await select(new File(["x"], "notes.html", { type: "text/html" }));
  expect(uploadButton()).toBeDisabled();
  await select(pdf());

  expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  expect(uploadButton()).toBeEnabled();
});

test("the chosen file can be removed", async () => {
  renderDialog();
  await select(pdf());

  await userEvent.setup().click(screen.getByRole("button", { name: "Remove report.pdf" }));

  expect(screen.queryByText("report.pdf")).not.toBeInTheDocument();
  expect(uploadButton()).toBeDisabled();
});

test("accepts a file dropped onto the drop area", async () => {
  renderDialog();
  const zone = (await screen.findByText(/Drag a file here/)).closest("label")!;
  const file = pdf("dropped.pdf");

  fireEvent.dragOver(zone, { dataTransfer: { files: [file] } });
  expect(screen.getByText("Drop the file to select it")).toBeInTheDocument();
  fireEvent.drop(zone, { dataTransfer: { files: [file] } });

  expect(screen.getByText("dropped.pdf")).toBeInTheDocument();
  expect(uploadButton()).toBeEnabled();
});

test("validates a dropped file too, since a drop bypasses the picker's filter", async () => {
  renderDialog();
  const zone = (await screen.findByText(/Drag a file here/)).closest("label")!;

  fireEvent.drop(zone, { dataTransfer: { files: [new File(["MZ"], "malware.exe")] } });

  expect(screen.getByRole("alert")).toHaveTextContent("Only PDF, DOCX and TXT files");
  expect(uploadButton()).toBeDisabled();
});

test("refuses several files dropped at once", async () => {
  renderDialog();
  const zone = (await screen.findByText(/Drag a file here/)).closest("label")!;

  fireEvent.drop(zone, { dataTransfer: { files: [pdf("a.pdf"), pdf("b.pdf")] } });

  expect(screen.getByRole("alert")).toHaveTextContent("Upload one file at a time.");
  expect(uploadButton()).toBeDisabled();
});

test("shows progress while uploading and blocks a second upload", async () => {
  let report: UploadRequest["onProgress"];
  let finish: (value: unknown) => void = () => {};
  mocks.uploadDocument.mockImplementation((_workspace, _file, request: UploadRequest) => {
    report = request.onProgress;
    return new Promise((resolve) => (finish = resolve));
  });
  const { onOpenChange } = renderDialog();
  await select(pdf());
  await userEvent.setup().click(uploadButton());

  const bar = await screen.findByRole("progressbar", { name: "Uploading…" });
  expect(bar).toHaveAttribute("aria-valuenow", "0");
  report?.(0.42);
  await vi.waitFor(() => expect(bar).toHaveAttribute("aria-valuenow", "42"));
  expect(screen.getByText("42%")).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Uploading…" })).toBeDisabled();
  expect(fileInput()).toBeDisabled();

  finish({ id: "doc-1" });
  await vi.waitFor(() => expect(onOpenChange).toHaveBeenCalledWith(false));
  expect(mocks.uploadDocument).toHaveBeenCalledTimes(1);
});

test("shows the server's reason when it rejects the file, and keeps the dialog open to retry", async () => {
  mocks.uploadDocument.mockRejectedValueOnce(
    new ApiError(415, "content_mismatch", "The file's contents do not match its extension."),
  );
  const { onOpenChange } = renderDialog();
  await select(pdf());
  const user = userEvent.setup();

  await user.click(uploadButton());

  expect(await screen.findByRole("alert")).toHaveTextContent(
    "The file's contents do not match its extension.",
  );
  expect(onOpenChange).not.toHaveBeenCalledWith(false);
  expect(screen.getByText("report.pdf")).toBeInTheDocument();

  await user.click(uploadButton());
  await vi.waitFor(() => expect(onOpenChange).toHaveBeenCalledWith(false));
});

test.each([
  [
    new ApiError(409, "duplicate_document", "This file is already in the workspace as “report.pdf”."),
    "already in the workspace",
  ],
  [new ApiError(413, "file_too_large", "Files can be up to 25 MB."), "up to 25 MB"],
  [
    new ApiError(502, "storage_unavailable", "File storage is temporarily unavailable."),
    "temporarily unavailable",
  ],
  [new ApiError(0, "network_error", "Could not reach the server."), "Could not reach the server."],
])("explains the failure %#", async (error, expected) => {
  mocks.uploadDocument.mockRejectedValue(error);
  renderDialog();
  await select(pdf());

  await userEvent.setup().click(uploadButton());

  expect(await screen.findByRole("alert")).toHaveTextContent(expected);
});

test("cancelling during an upload aborts it and closes without an error", async () => {
  let signal: AbortSignal | undefined;
  mocks.uploadDocument.mockImplementation(
    (_workspace, _file, request: UploadRequest) =>
      new Promise((_resolve, reject) => {
        signal = request.signal;
        request.signal?.addEventListener("abort", () => reject(new DOMException("Cancelled", "AbortError")));
      }),
  );
  const { onOpenChange } = renderDialog();
  await select(pdf());
  const user = userEvent.setup();
  await user.click(uploadButton());
  await screen.findByRole("progressbar");

  await user.click(screen.getByRole("button", { name: "Cancel" }));

  expect(signal?.aborted).toBe(true);
  expect(onOpenChange).toHaveBeenCalledWith(false);
  expect(screen.queryByRole("alert")).not.toBeInTheDocument();
});
