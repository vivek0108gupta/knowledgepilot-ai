import { QueryClientProvider } from "@tanstack/react-query";
import { screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, test, vi } from "vitest";

import { ApiError } from "@/lib/api";
import type { DocumentItem } from "@/lib/documents";
import type { SearchResponse, SearchResult } from "@/lib/search";
import type { Role, Workspace } from "@/lib/workspaces";
import { renderWithQuery } from "@/test/render";

import { WorkspaceSearch } from "./workspace-search";

const WORKSPACE_ID = "8a5c1f0e-0000-4000-8000-000000000000";
const OTHER_WORKSPACE_ID = "9b6d2a1f-0000-4000-8000-000000000000";
const REFUND_DOC = "33333333-3333-4333-8333-333333333333";
const NOTES_DOC = "55555555-5555-4555-8555-555555555555";

const mocks = vi.hoisted(() => ({
  searchWorkspace: vi.fn(),
  listSearchableDocuments: vi.fn(),
  getDownload: vi.fn(),
  startDownload: vi.fn(),
}));

vi.mock("@/lib/search", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/search")>()),
  searchWorkspace: mocks.searchWorkspace,
  listSearchableDocuments: mocks.listSearchableDocuments,
}));

vi.mock("@/lib/documents", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/documents")>()),
  getDownload: mocks.getDownload,
  startDownload: mocks.startDownload,
}));

function workspace(id = WORKSPACE_ID, role: Role = "member"): Workspace {
  return {
    id,
    name: "Research",
    owner_id: "11111111-1111-4111-8111-111111111111",
    role,
    created_at: "2026-11-11T00:00:00Z",
    updated_at: "2026-11-11T00:00:00Z",
  };
}

function document(id: string, filename: string): DocumentItem {
  return {
    id,
    workspace_id: WORKSPACE_ID,
    uploaded_by: "11111111-1111-4111-8111-111111111111",
    filename,
    mime_type: "application/pdf",
    size_bytes: 2048,
    status: "ready",
    page_count: 3,
    error: null,
    created_at: "2026-11-11T00:00:00Z",
    updated_at: "2026-11-11T00:00:00Z",
  };
}

let counter = 0;
function result(overrides: Partial<SearchResult> = {}): SearchResult {
  counter += 1;
  return {
    chunk_id: `00000000-0000-4000-8000-${String(counter).padStart(12, "0")}`,
    document_id: REFUND_DOC,
    document_name: "refund-policy.pdf",
    content: "Customers can request a refund within 30 days of purchase.",
    similarity: 0.87,
    source: { page_number: 4, paragraph_index: null, line_start: null, line_end: null },
    ...overrides,
  };
}

function response(results: SearchResult[], query = "refund policy"): SearchResponse {
  return { query, results };
}

function renderSearch(id = WORKSPACE_ID) {
  return renderWithQuery(<WorkspaceSearch workspace={workspace(id)} />);
}

const input = () => screen.getByRole("searchbox", { name: "Search this workspace's documents" });
const button = () => screen.getByRole("button", { name: "Search" });

async function run(query: string) {
  const user = userEvent.setup();
  if (query) await user.type(input(), query);
  await user.click(button());
  return user;
}

beforeEach(() => {
  counter = 0;
  for (const mock of Object.values(mocks)) mock.mockReset();
  mocks.listSearchableDocuments.mockResolvedValue([
    document(REFUND_DOC, "refund-policy.pdf"),
    document(NOTES_DOC, "notes.docx"),
  ]);
});

describe("searching", () => {
  test("has a labelled search box with a placeholder, a search button, and no results yet", () => {
    renderSearch();

    expect(screen.getByRole("search")).toBeInTheDocument();
    expect(input()).toHaveAttribute("placeholder", expect.stringContaining("What is the refund policy?"));
    expect(button()).toBeEnabled();
    expect(screen.queryByRole("list")).not.toBeInTheDocument();
  });

  test("shows matching passages with document name, excerpt, score and source, best first", async () => {
    mocks.searchWorkspace.mockResolvedValue(
      response([
        result({ similarity: 0.8749 }),
        result({
          document_id: NOTES_DOC,
          document_name: "notes.docx",
          content: "Refunds are processed by the finance team.",
          similarity: 0.52,
          source: { page_number: null, paragraph_index: 12, line_start: null, line_end: null },
        }),
      ]),
    );
    renderSearch();

    await run("refund policy");

    const cards = await screen.findAllByRole("listitem");
    expect(cards).toHaveLength(2);
    expect(within(cards[0]!).getByRole("heading")).toHaveTextContent("refund-policy.pdf");
    expect(cards[0]).toHaveTextContent("Customers can request a refund within 30 days of purchase.");
    expect(cards[0]).toHaveTextContent("87% match");
    expect(cards[0]).toHaveTextContent("Page 4");
    expect(within(cards[1]!).getByRole("heading")).toHaveTextContent("notes.docx");
    expect(cards[1]).toHaveTextContent("52% match");
    expect(screen.getByText(/2 passages for “refund policy”/)).toBeInTheDocument();
  });

  test("sends the trimmed query for this workspace, across all documents by default", async () => {
    mocks.searchWorkspace.mockResolvedValue(response([result()]));
    renderSearch();

    await run("   refund policy  ");

    await vi.waitFor(() => expect(mocks.searchWorkspace).toHaveBeenCalledTimes(1));
    expect(mocks.searchWorkspace.mock.calls[0]!.slice(0, 2)).toEqual([
      WORKSPACE_ID,
      { query: "refund policy", documentIds: [] },
    ]);
  });

  test("pressing Enter searches too", async () => {
    mocks.searchWorkspace.mockResolvedValue(response([result()]));
    renderSearch();

    await userEvent.setup().type(input(), "refund policy{Enter}");

    expect(await screen.findByRole("listitem")).toBeInTheDocument();
  });

  test("an empty search is stopped before any request, with a message", async () => {
    renderSearch();

    await run("");

    expect(screen.getByRole("alert")).toHaveTextContent("Enter something to search for.");
    expect(input()).toBeInvalid();
    expect(mocks.searchWorkspace).not.toHaveBeenCalled();
  });

  test("the box cannot hold more than the 1,000 characters the API accepts", () => {
    renderSearch();

    expect(input()).toHaveAttribute("maxlength", "1000");
  });
});

describe("loading state", () => {
  test("shows progress and blocks a second search until the first answers", async () => {
    let finish: (value: SearchResponse) => void = () => {};
    mocks.searchWorkspace.mockReturnValue(new Promise((resolve) => (finish = resolve)));
    renderSearch();

    await run("refund policy");

    expect(await screen.findByRole("status", { name: "Searching documents" })).toBeInTheDocument();
    const searching = screen.getByRole("button", { name: "Searching…" });
    expect(searching).toBeDisabled();
    expect(searching).toHaveAttribute("aria-busy", "true");

    finish(response([result()]));

    expect(await screen.findByRole("listitem")).toBeInTheDocument();
    expect(screen.queryByRole("status", { name: "Searching documents" })).not.toBeInTheDocument();
    expect(button()).toBeEnabled();
  });
});

describe("empty results", () => {
  test("says nothing matched and suggests what to try", async () => {
    mocks.searchWorkspace.mockResolvedValue(response([], "zebra quantum"));
    renderSearch();

    await run("zebra quantum");

    expect(await screen.findByRole("heading", { name: "No matching passages" })).toBeInTheDocument();
    expect(screen.getByText(/Nothing in this workspace matches “zebra quantum”/)).toBeInTheDocument();
    expect(screen.queryByRole("list")).not.toBeInTheDocument();
  });

  test("a workspace with no processed documents says to upload one first", async () => {
    mocks.listSearchableDocuments.mockResolvedValue([]);
    renderSearch();

    expect(await screen.findByRole("heading", { name: "Nothing to search yet" })).toBeInTheDocument();
    expect(screen.getByLabelText("Search in")).toBeDisabled();
  });

  test("a search of an empty workspace explains why there are no results", async () => {
    mocks.listSearchableDocuments.mockResolvedValue([]);
    mocks.searchWorkspace.mockResolvedValue(response([], "refund"));
    renderSearch();
    await screen.findByRole("heading", { name: "Nothing to search yet" });

    await run("refund");

    expect(await screen.findByText(/no processed documents yet/)).toBeInTheDocument();
  });
});

describe("search failures", () => {
  test.each([
    [
      new ApiError(500, "internal_error", "boom"),
      "Search is temporarily unavailable. Try again in a moment.",
      true,
    ],
    [
      new ApiError(503, "embeddings_unavailable", "x"),
      "Search is temporarily unavailable. Try again in a moment.",
      true,
    ],
    [
      new ApiError(429, "too_many_requests", "x"),
      "Too many searches in a short time. Wait a moment and try again.",
      true,
    ],
    [
      new ApiError(0, "network_error", "x"),
      "Could not reach the server. Check your connection and try again.",
      true,
    ],
    [
      new ApiError(422, "validation_error", "x"),
      "That search could not be run. Shorten or rephrase it and try again.",
      false,
    ],
    [
      new ApiError(503, "embeddings_not_configured", "x"),
      "Search has not been set up on the server yet.",
      false,
    ],
  ])("%s shows a clear message", async (error, message, retryable) => {
    mocks.searchWorkspace.mockRejectedValue(error);
    renderSearch();

    await run("refund policy");

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("Search failed");
    expect(alert).toHaveTextContent(message);
    // Retry is offered only when trying again could help.
    expect(within(alert).queryByRole("button", { name: "Try again" }) !== null).toBe(retryable);
    expect(screen.queryByRole("list")).not.toBeInTheDocument();
  });

  test("the server's raw error text is not shown", async () => {
    mocks.searchWorkspace.mockRejectedValue(
      new ApiError(500, "internal_error", "Traceback: db-internal:5432"),
    );
    renderSearch();

    await run("refund policy");

    expect(await screen.findByRole("alert")).not.toHaveTextContent("db-internal");
  });

  test("Try again repeats the same search and shows its results", async () => {
    mocks.searchWorkspace
      .mockRejectedValueOnce(new ApiError(503, "embeddings_unavailable", "x"))
      .mockResolvedValueOnce(response([result()]));
    renderSearch();
    const user = await run("refund policy");

    await user.click(await screen.findByRole("button", { name: "Try again" }));

    expect(await screen.findByRole("listitem")).toBeInTheDocument();
    expect(mocks.searchWorkspace.mock.calls[1]!.slice(0, 2)).toEqual(
      mocks.searchWorkspace.mock.calls[0]!.slice(0, 2),
    );
  });

  test("a failed search clears the previous results instead of leaving them on screen", async () => {
    mocks.searchWorkspace
      .mockResolvedValueOnce(response([result()]))
      .mockRejectedValueOnce(new ApiError(500, "internal_error", "x"));
    renderSearch();
    const user = await run("refund policy");
    await screen.findByRole("listitem");

    await user.click(button());

    expect(await screen.findByRole("alert")).toBeInTheDocument();
    expect(screen.queryByRole("listitem")).not.toBeInTheDocument();
  });
});

describe("authentication and authorization errors", () => {
  test("an expired session tells the user to sign in again, with no retry", async () => {
    mocks.searchWorkspace.mockRejectedValue(
      new ApiError(401, "token_expired", "The access token has expired."),
    );
    renderSearch();

    await run("refund policy");

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("Your session has expired. Sign in again to search.");
    expect(within(alert).queryByRole("button", { name: "Try again" })).not.toBeInTheDocument();
  });

  test("a 403 says the user lacks permission", async () => {
    mocks.searchWorkspace.mockRejectedValue(new ApiError(403, "forbidden", "x"));
    renderSearch();

    await run("refund policy");

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "You do not have permission to search this workspace.",
    );
  });

  test("a workspace the user was removed from shows not found and no results", async () => {
    mocks.searchWorkspace.mockRejectedValue(new ApiError(404, "workspace_not_found", "Workspace not found."));
    renderSearch();

    await run("refund policy");

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("This workspace no longer exists, or you are no longer a member of it.");
    expect(within(alert).queryByRole("button", { name: "Try again" })).not.toBeInTheDocument();
    expect(screen.queryByRole("listitem")).not.toBeInTheDocument();
  });
});

describe("workspace isolation", () => {
  test("every request names only the workspace being viewed", async () => {
    mocks.searchWorkspace.mockResolvedValue(response([result()]));
    renderSearch(WORKSPACE_ID);

    await run("salary bands");

    await vi.waitFor(() => expect(mocks.searchWorkspace).toHaveBeenCalled());
    expect(mocks.listSearchableDocuments.mock.calls.every((call) => call[0] === WORKSPACE_ID)).toBe(true);
    expect(mocks.searchWorkspace.mock.calls.every((call) => call[0] === WORKSPACE_ID)).toBe(true);
  });

  test("the document filter offers only this workspace's documents", async () => {
    renderSearch();

    const filter = await screen.findByLabelText("Search in");
    await within(filter).findByRole("option", { name: "notes.docx" });

    expect(
      within(filter)
        .getAllByRole("option")
        .map((option) => option.textContent),
    ).toEqual(["All documents", "refund-policy.pdf", "notes.docx"]);
  });

  test("switching workspace discards the previous workspace's results and filter", async () => {
    mocks.searchWorkspace.mockResolvedValue(
      response([result({ content: "Alice's confidential salary bands." })], "salary bands"),
    );
    const view = renderSearch(WORKSPACE_ID);
    await run("salary bands");
    expect(await screen.findByText("Alice's confidential salary bands.")).toBeInTheDocument();

    // The workspace page mounts the search with the workspace ID as its key.
    mocks.listSearchableDocuments.mockResolvedValue([]);
    view.rerender(
      <QueryClientProvider client={view.queryClient}>
        <WorkspaceSearch key={OTHER_WORKSPACE_ID} workspace={workspace(OTHER_WORKSPACE_ID)} />
      </QueryClientProvider>,
    );

    expect(screen.queryByText("Alice's confidential salary bands.")).not.toBeInTheDocument();
    expect(input()).toHaveValue("");
    await vi.waitFor(() =>
      expect(mocks.listSearchableDocuments).toHaveBeenLastCalledWith(OTHER_WORKSPACE_ID, expect.anything()),
    );
  });

  test("a second search cannot be started while one is running, by button or by Enter", async () => {
    mocks.searchWorkspace.mockReturnValue(new Promise(() => {}));
    renderSearch();
    const user = userEvent.setup();
    await user.type(input(), "first{Enter}");
    await screen.findByRole("status", { name: "Searching documents" });

    await user.type(input(), " again{Enter}");

    expect(mocks.searchWorkspace).toHaveBeenCalledTimes(1);
  });

  test("leaving the page cancels a search that is still running", async () => {
    let signal: AbortSignal | undefined;
    mocks.searchWorkspace.mockImplementation((_id, _input, received: AbortSignal) => {
      signal = received;
      return new Promise(() => {});
    });
    const view = renderSearch();
    await run("salary bands");
    await screen.findByRole("status", { name: "Searching documents" });

    view.unmount();

    expect(signal?.aborted).toBe(true);
  });
});

describe("result source references", () => {
  async function sourceOf(source: SearchResult["source"]) {
    mocks.searchWorkspace.mockResolvedValue(response([result({ source })]));
    renderSearch();
    await run("refund policy");
    return screen.findByRole("listitem");
  }

  const none = { page_number: null, paragraph_index: null, line_start: null, line_end: null };

  test("a PDF result shows its page", async () => {
    expect(await sourceOf({ ...none, page_number: 4 })).toHaveTextContent("Page 4");
  });

  test("a Word result shows its paragraph and no page", async () => {
    const card = await sourceOf({ ...none, paragraph_index: 12 });

    expect(card).toHaveTextContent("Paragraph 13");
    expect(card).not.toHaveTextContent(/Page \d/);
  });

  test("a text result shows its line range", async () => {
    expect(await sourceOf({ ...none, line_start: 10, line_end: 24 })).toHaveTextContent("Lines 10–24");
  });

  test("a result with no reference shows none", async () => {
    const card = await sourceOf(none);

    expect(card).not.toHaveTextContent(/Page|Paragraph|Line/);
  });
});

describe("document filtering", () => {
  test("choosing a document restricts the search to it", async () => {
    mocks.searchWorkspace.mockResolvedValue(
      response([result({ document_id: NOTES_DOC, document_name: "notes.docx" })]),
    );
    renderSearch();
    const user = userEvent.setup();
    const filter = await screen.findByLabelText("Search in");
    await within(filter).findByRole("option", { name: "notes.docx" });

    await user.selectOptions(filter, "notes.docx");
    await user.type(input(), "refund policy");
    await user.click(button());

    await vi.waitFor(() => expect(mocks.searchWorkspace).toHaveBeenCalled());
    expect(mocks.searchWorkspace.mock.calls[0]![1]).toEqual({
      query: "refund policy",
      documentIds: [NOTES_DOC],
    });
    expect(await screen.findByText(/in “notes.docx”/)).toBeInTheDocument();
  });

  test("no match inside one document suggests searching all documents", async () => {
    mocks.searchWorkspace.mockResolvedValue(response([], "holiday"));
    renderSearch();
    const user = userEvent.setup();
    const filter = await screen.findByLabelText("Search in");
    await within(filter).findByRole("option", { name: "notes.docx" });

    await user.selectOptions(filter, "notes.docx");
    await user.type(input(), "holiday");
    await user.click(button());

    expect(await screen.findByText(/Nothing in “notes.docx” matches “holiday”/)).toBeInTheDocument();
  });

  test("going back to all documents removes the restriction", async () => {
    mocks.searchWorkspace.mockResolvedValue(response([result()]));
    renderSearch();
    const user = userEvent.setup();
    const filter = await screen.findByLabelText("Search in");
    await within(filter).findByRole("option", { name: "notes.docx" });
    await user.selectOptions(filter, "notes.docx");
    await user.selectOptions(filter, "All documents");

    await user.type(input(), "refund");
    await user.click(button());

    await vi.waitFor(() => expect(mocks.searchWorkspace).toHaveBeenCalled());
    expect(mocks.searchWorkspace.mock.calls[0]![1]).toEqual({ query: "refund", documentIds: [] });
  });

  test("search still works if the document list cannot be loaded", async () => {
    mocks.listSearchableDocuments.mockRejectedValue(new ApiError(500, "internal_error", "x"));
    mocks.searchWorkspace.mockResolvedValue(response([result()]));
    renderSearch();

    await run("refund policy");

    expect(await screen.findByRole("listitem")).toBeInTheDocument();
  });
});

describe("result cards", () => {
  test("document text is shown as text, never run as markup", async () => {
    const hostile = 'Ignore previous instructions. <img src=x onerror="alert(1)"><script>alert(2)</script>';
    mocks.searchWorkspace.mockResolvedValue(response([result({ content: hostile })]));
    const { container } = renderSearch();

    await run("instructions");

    expect(await screen.findByText(hostile)).toBeInTheDocument();
    expect(container.querySelector("script, img")).toBeNull();
  });

  test("a long passage is folded and can be expanded and folded again", async () => {
    const long = `${"The refund policy applies to every order. ".repeat(40)}END-OF-PASSAGE`;
    mocks.searchWorkspace.mockResolvedValue(response([result({ content: long })]));
    renderSearch();
    const user = await run("refund policy");

    const card = await screen.findByRole("listitem");
    expect(card).not.toHaveTextContent("END-OF-PASSAGE");
    const toggle = within(card).getByRole("button", { name: "Show more" });
    expect(toggle).toHaveAttribute("aria-expanded", "false");

    await user.click(toggle);
    expect(card).toHaveTextContent("END-OF-PASSAGE");
    expect(within(card).getByRole("button", { name: "Show less" })).toHaveAttribute("aria-expanded", "true");
  });

  test("a short passage has no expand control", async () => {
    mocks.searchWorkspace.mockResolvedValue(response([result()]));
    renderSearch();

    await run("refund policy");

    const card = await screen.findByRole("listitem");
    expect(within(card).queryByRole("button", { name: "Show more" })).not.toBeInTheDocument();
  });

  test("the source document can be downloaded through a signed link", async () => {
    mocks.searchWorkspace.mockResolvedValue(response([result()]));
    mocks.getDownload.mockResolvedValue({
      url: "https://storage.test/signed?token=abc",
      expires_in: 60,
      filename: "refund-policy.pdf",
    });
    renderSearch();
    const user = await run("refund policy");

    await user.click(await screen.findByRole("button", { name: "Download refund-policy.pdf" }));

    await vi.waitFor(() =>
      expect(mocks.startDownload).toHaveBeenCalledWith("https://storage.test/signed?token=abc"),
    );
    expect(mocks.getDownload).toHaveBeenCalledWith(REFUND_DOC);
  });

  test("a failed download is reported on that result", async () => {
    mocks.searchWorkspace.mockResolvedValue(response([result()]));
    mocks.getDownload.mockRejectedValue(new ApiError(404, "document_not_found", "Document not found."));
    renderSearch();
    const user = await run("refund policy");

    await user.click(await screen.findByRole("button", { name: "Download refund-policy.pdf" }));

    const card = await screen.findByRole("listitem");
    expect(await within(card).findByRole("alert")).toHaveTextContent("Document not found.");
  });

  test("a single result is counted in the singular", async () => {
    mocks.searchWorkspace.mockResolvedValue(response([result()]));
    renderSearch();

    await run("refund policy");

    expect(await screen.findByText(/^1 passage for “refund policy”/)).toBeInTheDocument();
  });
});
