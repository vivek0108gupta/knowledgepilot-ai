import { screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, test, vi } from "vitest";

import { ApiError } from "@/lib/api";
import type { DocumentItem, DocumentPage, DocumentStatus } from "@/lib/documents";
import type { Role, Workspace } from "@/lib/workspaces";
import { renderWithQuery } from "@/test/render";

import { Documents } from "./documents";

const ME = "11111111-1111-4111-8111-111111111111";
const OTHER = "22222222-2222-4222-8222-222222222222";
const WORKSPACE_ID = "8a5c1f0e-0000-4000-8000-000000000000";

const mocks = vi.hoisted(() => ({
  listDocuments: vi.fn(),
  getDownload: vi.fn(),
  deleteDocument: vi.fn(),
  retryDocument: vi.fn(),
  startDownload: vi.fn(),
  uploadDocument: vi.fn(),
}));

vi.mock("@/lib/documents", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/documents")>()),
  ...mocks,
}));

vi.mock("@/components/auth/auth-provider", () => ({
  useAuth: () => ({ status: "authenticated", user: { id: ME, email: "ada@example.com" }, signOut: vi.fn() }),
}));

function workspace(role: Role = "owner"): Workspace {
  return {
    id: WORKSPACE_ID,
    name: "Research",
    owner_id: ME,
    role,
    created_at: "2026-11-11T00:00:00Z",
    updated_at: "2026-11-11T00:00:00Z",
  };
}

let counter = 0;
function doc(overrides: Partial<DocumentItem> = {}): DocumentItem {
  counter += 1;
  return {
    id: `00000000-0000-4000-8000-${String(counter).padStart(12, "0")}`,
    workspace_id: WORKSPACE_ID,
    uploaded_by: ME,
    filename: `file-${counter}.pdf`,
    mime_type: "application/pdf",
    size_bytes: 2048,
    status: "ready",
    page_count: 3,
    error: null,
    created_at: "2026-11-11T09:30:00Z",
    updated_at: "2026-11-11T09:31:00Z",
    ...overrides,
  };
}

function page(items: DocumentItem[], total = items.length, offset = 0): DocumentPage {
  return { items, total, limit: 10, offset };
}

function renderDocuments(role: Role = "owner") {
  return renderWithQuery(<Documents workspace={workspace(role)} />);
}

beforeEach(() => {
  counter = 0;
  for (const mock of Object.values(mocks)) mock.mockReset();
});

describe("document list", () => {
  test("shows a loading state, then each document's name, type, size and status", async () => {
    mocks.listDocuments.mockResolvedValue(
      page([
        doc({ filename: "report.pdf", size_bytes: 1536, status: "ready" }),
        doc({
          filename: "notes.docx",
          mime_type: "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
          size_bytes: 5 * 1024 * 1024,
          status: "processing",
        }),
        doc({ filename: "log.txt", mime_type: "text/plain", status: "failed", error: "Bad file." }),
        doc({ filename: "queued.pdf", status: "uploaded" }),
      ]),
    );
    renderDocuments();

    expect(screen.getByRole("status", { name: "Loading documents" })).toBeInTheDocument();

    const table = await screen.findByRole("table", { name: "Documents in this workspace" });
    const rows = within(table).getAllByRole("row").slice(1);
    expect(rows.map((row) => within(row).getByRole("rowheader").textContent)).toEqual([
      expect.stringContaining("report.pdf"),
      expect.stringContaining("notes.docx"),
      expect.stringContaining("log.txt"),
      expect.stringContaining("queued.pdf"),
    ]);
    expect(rows[0]).toHaveTextContent("PDF");
    expect(rows[0]).toHaveTextContent("1.5 KB");
    expect(rows[0]).toHaveTextContent("Ready");
    expect(rows[1]).toHaveTextContent("Word");
    expect(rows[1]).toHaveTextContent("5.0 MB");
    expect(rows[1]).toHaveTextContent("Processing");
    expect(rows[2]).toHaveTextContent("Text");
    expect(rows[2]).toHaveTextContent("Failed");
    expect(rows[3]).toHaveTextContent("Queued");
    expect(screen.getByText("4 documents")).toBeInTheDocument();
    expect(screen.queryByRole("status", { name: "Loading documents" })).not.toBeInTheDocument();
  });

  test("asks for this workspace's first page with no filter", async () => {
    mocks.listDocuments.mockResolvedValue(page([doc()]));
    renderDocuments();
    await screen.findByRole("table");

    expect(mocks.listDocuments.mock.calls[0]!.slice(0, 2)).toEqual([
      WORKSPACE_ID,
      { status: "all", page: 0 },
    ]);
  });

  test("shows an empty state that opens the upload dialog", async () => {
    mocks.listDocuments.mockResolvedValue(page([]));
    renderDocuments();

    expect(await screen.findByRole("heading", { name: "No documents yet" })).toBeInTheDocument();
    const emptyState = screen.getByRole("heading", { name: "No documents yet" }).parentElement!;
    await userEvent.setup().click(within(emptyState).getByRole("button", { name: "Upload document" }));

    expect(await screen.findByRole("dialog", { name: "Upload document" })).toBeInTheDocument();
  });

  test("shows the error and retries on request", async () => {
    mocks.listDocuments.mockRejectedValueOnce(
      new ApiError(0, "network_error", "Could not reach the server."),
    );
    mocks.listDocuments.mockResolvedValueOnce(page([doc({ filename: "report.pdf" })]));
    renderDocuments();

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("Could not load documents");
    expect(alert).toHaveTextContent("Could not reach the server.");
    await userEvent.setup().click(screen.getByRole("button", { name: "Try again" }));

    expect(await screen.findByRole("button", { name: "report.pdf" })).toBeInTheDocument();
  });

  test("filters by status, and says so when nothing matches", async () => {
    mocks.listDocuments.mockImplementation(async (_id, query: { status: DocumentStatus | "all" }) =>
      query.status === "failed" ? page([]) : page([doc({ filename: "report.pdf" })]),
    );
    const user = userEvent.setup();
    renderDocuments();
    await screen.findByRole("table");

    await user.selectOptions(screen.getByLabelText("Status"), "failed");

    expect(await screen.findByRole("heading", { name: "No documents with this status" })).toBeInTheDocument();
    expect(mocks.listDocuments.mock.calls.at(-1)!.slice(0, 2)).toEqual([
      WORKSPACE_ID,
      { status: "failed", page: 0 },
    ]);

    await user.click(screen.getByRole("button", { name: "Show all" }));
    expect(await screen.findByRole("button", { name: "report.pdf" })).toBeInTheDocument();
  });

  test("pages through long lists", async () => {
    mocks.listDocuments.mockImplementation(async (_id, query: { page: number }) =>
      query.page === 0
        ? page([doc({ filename: "first-page.pdf" })], 15, 0)
        : page([doc({ filename: "second-page.pdf" })], 15, 10),
    );
    const user = userEvent.setup();
    renderDocuments();

    expect(await screen.findByText("15 documents · Page 1 of 2")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Previous" })).toBeDisabled();

    await user.click(screen.getByRole("button", { name: "Next" }));

    expect(await screen.findByRole("button", { name: "second-page.pdf" })).toBeInTheDocument();
    expect(screen.getByText("15 documents · Page 2 of 2")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Next" })).toBeDisabled();
  });

  test("keeps checking while a document is processing, and stops once all are settled", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    try {
      const processing = doc({ filename: "report.pdf", status: "processing" });
      mocks.listDocuments
        .mockResolvedValueOnce(page([processing]))
        .mockResolvedValue(page([{ ...processing, status: "ready" }]));
      renderDocuments();
      // Scoped to the table: the status filter also lists these words as options.
      const table = await screen.findByRole("table");
      expect(within(table).getByText("Processing")).toBeInTheDocument();

      await vi.advanceTimersByTimeAsync(3000);
      expect(await within(table).findByText("Ready")).toBeInTheDocument();
      const callsWhenSettled = mocks.listDocuments.mock.calls.length;

      await vi.advanceTimersByTimeAsync(10_000);
      expect(mocks.listDocuments.mock.calls.length).toBe(callsWhenSettled);
    } finally {
      vi.useRealTimers();
    }
  });
});

describe("permissions", () => {
  const mine = () => doc({ filename: "mine.pdf", uploaded_by: ME });
  const theirs = () => doc({ filename: "theirs.pdf", uploaded_by: OTHER });

  test.each(["owner", "admin"] as const)("an %s can delete anyone's document", async (role) => {
    mocks.listDocuments.mockResolvedValue(page([mine(), theirs()]));
    renderDocuments(role);

    expect(await screen.findByRole("button", { name: "Delete mine.pdf" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Delete theirs.pdf" })).toBeInTheDocument();
  });

  test("a member can delete only their own upload, but download anything", async () => {
    mocks.listDocuments.mockResolvedValue(page([mine(), theirs()]));
    renderDocuments("member");

    expect(await screen.findByRole("button", { name: "Delete mine.pdf" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Delete theirs.pdf" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Download theirs.pdf" })).toBeInTheDocument();
  });
});

describe("download", () => {
  test("fetches a signed link for that document and starts the download", async () => {
    const target = doc({ filename: "report.pdf" });
    mocks.listDocuments.mockResolvedValue(page([target, doc()]));
    mocks.getDownload.mockResolvedValue({
      url: "https://storage.test/signed?token=abc",
      expires_in: 60,
      filename: "report.pdf",
    });
    renderDocuments();

    await userEvent.setup().click(await screen.findByRole("button", { name: "Download report.pdf" }));

    await vi.waitFor(() =>
      expect(mocks.startDownload).toHaveBeenCalledWith("https://storage.test/signed?token=abc"),
    );
    expect(mocks.getDownload).toHaveBeenCalledWith(target.id);
  });

  test("shows the reason when a link cannot be created", async () => {
    mocks.listDocuments.mockResolvedValue(page([doc({ filename: "report.pdf" })]));
    mocks.getDownload.mockRejectedValue(
      new ApiError(502, "storage_unavailable", "File storage is temporarily unavailable."),
    );
    renderDocuments();

    await userEvent.setup().click(await screen.findByRole("button", { name: "Download report.pdf" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("File storage is temporarily unavailable.");
    expect(mocks.startDownload).not.toHaveBeenCalled();
  });
});

describe("delete confirmation", () => {
  test("asks before deleting, and deletes only after confirming", async () => {
    const target = doc({ filename: "report.pdf" });
    mocks.listDocuments.mockResolvedValue(page([target]));
    mocks.deleteDocument.mockResolvedValue(undefined);
    const user = userEvent.setup();
    const { queryClient } = renderDocuments();
    const invalidate = vi.spyOn(queryClient, "invalidateQueries");

    await user.click(await screen.findByRole("button", { name: "Delete report.pdf" }));

    const dialog = await screen.findByRole("dialog", { name: "Delete “report.pdf”?" });
    expect(dialog).toHaveTextContent("It cannot be undone.");
    expect(mocks.deleteDocument).not.toHaveBeenCalled();

    await user.click(within(dialog).getByRole("button", { name: "Delete document" }));

    await vi.waitFor(() => expect(mocks.deleteDocument).toHaveBeenCalledWith(target.id));
    await vi.waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    expect(invalidate).toHaveBeenCalledWith({ queryKey: ["workspaces", WORKSPACE_ID, "documents"] });
  });

  test("cancelling keeps the document", async () => {
    mocks.listDocuments.mockResolvedValue(page([doc({ filename: "report.pdf" })]));
    const user = userEvent.setup();
    renderDocuments();

    await user.click(await screen.findByRole("button", { name: "Delete report.pdf" }));
    const dialog = await screen.findByRole("dialog");
    await user.click(within(dialog).getByRole("button", { name: "Cancel" }));

    await vi.waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    expect(mocks.deleteDocument).not.toHaveBeenCalled();
    expect(screen.getByRole("button", { name: "report.pdf" })).toBeInTheDocument();
  });

  test("shows the reason when deleting fails, and stays open", async () => {
    mocks.listDocuments.mockResolvedValue(page([doc({ filename: "report.pdf" })]));
    mocks.deleteDocument.mockRejectedValue(
      new ApiError(403, "forbidden", "You do not have permission to do this."),
    );
    const user = userEvent.setup();
    renderDocuments();

    await user.click(await screen.findByRole("button", { name: "Delete report.pdf" }));
    const dialog = await screen.findByRole("dialog");
    await user.click(within(dialog).getByRole("button", { name: "Delete document" }));

    expect(await within(dialog).findByRole("alert")).toHaveTextContent(
      "You do not have permission to do this.",
    );
    expect(screen.getByRole("dialog")).toBeInTheDocument();
  });
});

describe("document details", () => {
  test("shows the document's details", async () => {
    mocks.listDocuments.mockResolvedValue(
      page([doc({ filename: "report.pdf", size_bytes: 1536, page_count: 12 })]),
    );
    renderDocuments();

    await userEvent.setup().click(await screen.findByRole("button", { name: "report.pdf" }));

    const dialog = await screen.findByRole("dialog", { name: "report.pdf" });
    expect(dialog).toHaveTextContent("PDF");
    expect(dialog).toHaveTextContent("1.5 KB");
    expect(dialog).toHaveTextContent("Ready");
    expect(dialog).toHaveTextContent("12");
    expect(dialog).toHaveTextContent("You");
    expect(within(dialog).getByRole("button", { name: "Download" })).toBeInTheDocument();
  });

  test("explains why a document failed and lets its uploader try again", async () => {
    const failed = doc({
      filename: "scan.pdf",
      status: "failed",
      page_count: null,
      error: "No text could be found in this PDF. OCR is not supported.",
    });
    mocks.listDocuments.mockResolvedValue(page([failed]));
    mocks.retryDocument.mockResolvedValue({ ...failed, status: "uploaded", error: null });
    const user = userEvent.setup();
    renderDocuments("member");

    await user.click(await screen.findByRole("button", { name: "scan.pdf" }));
    const dialog = await screen.findByRole("dialog", { name: "scan.pdf" });
    expect(within(dialog).getByRole("alert")).toHaveTextContent("OCR is not supported.");

    await user.click(within(dialog).getByRole("button", { name: "Try again" }));

    await vi.waitFor(() => expect(mocks.retryDocument).toHaveBeenCalledWith(failed.id));
  });

  test("a member viewing someone else's failed document gets no retry or delete", async () => {
    mocks.listDocuments.mockResolvedValue(
      page([doc({ filename: "theirs.pdf", uploaded_by: OTHER, status: "failed", error: "Bad file." })]),
    );
    renderDocuments("member");

    await userEvent.setup().click(await screen.findByRole("button", { name: "theirs.pdf" }));

    const dialog = await screen.findByRole("dialog", { name: "theirs.pdf" });
    expect(within(dialog).queryByRole("button", { name: "Try again" })).not.toBeInTheDocument();
    expect(within(dialog).queryByRole("button", { name: "Delete" })).not.toBeInTheDocument();
    expect(within(dialog).getByRole("button", { name: "Download" })).toBeInTheDocument();
    expect(dialog).toHaveTextContent(OTHER);
  });

  test("tells the user a document is still being prepared", async () => {
    mocks.listDocuments.mockResolvedValue(page([doc({ filename: "new.pdf", status: "processing" })]));
    renderDocuments();

    await userEvent.setup().click(await screen.findByRole("button", { name: "new.pdf" }));

    const dialog = await screen.findByRole("dialog", { name: "new.pdf" });
    expect(within(dialog).getByRole("status")).toHaveTextContent("being prepared");
  });

  test("delete from the details panel goes through the same confirmation", async () => {
    mocks.listDocuments.mockResolvedValue(page([doc({ filename: "report.pdf" })]));
    const user = userEvent.setup();
    renderDocuments();

    await user.click(await screen.findByRole("button", { name: "report.pdf" }));
    const details = await screen.findByRole("dialog", { name: "report.pdf" });
    await user.click(within(details).getByRole("button", { name: "Delete" }));

    expect(await screen.findByRole("dialog", { name: "Delete “report.pdf”?" })).toBeInTheDocument();
    expect(mocks.deleteDocument).not.toHaveBeenCalled();
  });
});
