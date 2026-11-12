import { QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, test, vi } from "vitest";

import { ApiError } from "@/lib/api";
import type { Citation, Conversation, Message, MessagePage } from "@/lib/conversations";
import { renderWithQuery } from "@/test/render";

import { ChatWorkspace } from "./chat";

const WORKSPACE = "8a5c1f0e-0000-4000-8000-000000000000";
const OTHER_WORKSPACE = "9b6d2a1f-0000-4000-8000-000000000000";
const FIRST = "11111111-1111-4111-8111-111111111111";
const SECOND = "22222222-2222-4222-8222-222222222222";

const mocks = vi.hoisted(() => ({
  params: {} as { workspaceId: string; conversationId?: string },
  push: vi.fn(),
  replace: vi.fn(),
  getWorkspace: vi.fn(),
  listConversations: vi.fn(),
  getConversation: vi.fn(),
  createConversation: vi.fn(),
  renameConversation: vi.fn(),
  deleteConversation: vi.fn(),
  listMessages: vi.fn(),
  askQuestion: vi.fn(),
  getDownload: vi.fn(),
  startDownload: vi.fn(),
}));

vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: mocks.push, replace: mocks.replace }),
  useParams: () => mocks.params,
}));

vi.mock("@/lib/documents", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/documents")>()),
  getDownload: mocks.getDownload,
  startDownload: mocks.startDownload,
}));

vi.mock("@/lib/workspaces", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/workspaces")>()),
  getWorkspace: mocks.getWorkspace,
}));

vi.mock("@/lib/conversations", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/conversations")>()),
  listConversations: mocks.listConversations,
  getConversation: mocks.getConversation,
  createConversation: mocks.createConversation,
  renameConversation: mocks.renameConversation,
  deleteConversation: mocks.deleteConversation,
  listMessages: mocks.listMessages,
  askQuestion: mocks.askQuestion,
}));

function conversation(id: string, title: string, workspaceId = WORKSPACE): Conversation {
  return {
    id,
    workspace_id: workspaceId,
    title,
    status: "active",
    message_count: 0,
    created_at: "2026-11-11T09:00:00Z",
    updated_at: "2026-11-11T09:00:00Z",
  };
}

function message(sequence: number, content: string, role: Message["role"] = "user"): Message {
  return {
    id: `00000000-0000-4000-8000-${String(sequence).padStart(12, "0")}`,
    conversation_id: FIRST,
    role,
    content,
    sequence_number: sequence,
    metadata: null,
    model: null,
    citations: [],
    created_at: "2026-11-11T09:30:00Z",
  };
}

const page = (items: Message[], hasMore = false): MessagePage => ({
  items,
  total: items.length,
  has_more: hasMore,
});
const list = (items: Conversation[]) => ({ items, total: items.length, limit: 100, offset: 0 });

function open(conversationId?: string, workspaceId = WORKSPACE) {
  mocks.params = { workspaceId, conversationId };
  return renderWithQuery(<ChatWorkspace />);
}

const composer = () => screen.getByRole("textbox", { name: "Message" });

beforeEach(() => {
  for (const mock of Object.values(mocks)) if (typeof mock === "function") mock.mockReset();
  mocks.params = { workspaceId: WORKSPACE };
  mocks.getWorkspace.mockResolvedValue({
    id: WORKSPACE,
    name: "Research",
    owner_id: "33333333-3333-4333-8333-333333333333",
    role: "member",
    created_at: "2026-11-11T00:00:00Z",
    updated_at: "2026-11-11T00:00:00Z",
  });
  mocks.listConversations.mockResolvedValue(
    list([conversation(FIRST, "Refund policy"), conversation(SECOND, "Shipping times")]),
  );
  mocks.getConversation.mockImplementation(async (id: string) =>
    conversation(id, id === FIRST ? "Refund policy" : "Shipping times"),
  );
  mocks.listMessages.mockResolvedValue(page([]));
});

describe("conversation list", () => {
  test("shows a loading state, then the user's conversations with links", async () => {
    open();

    const sidebar = await screen.findByRole("complementary", { name: "Conversations" });
    expect(within(sidebar).getByRole("status", { name: "Loading conversations" })).toBeInTheDocument();

    const first = await within(sidebar).findByRole("link", { name: "Refund policy" });
    expect(first).toHaveAttribute("href", `/dashboard/workspaces/${WORKSPACE}/chat/${FIRST}`);
    expect(within(sidebar).getByRole("link", { name: "Shipping times" })).toBeInTheDocument();
    expect(within(sidebar).queryByRole("status", { name: "Loading conversations" })).not.toBeInTheDocument();
    expect(mocks.listConversations.mock.calls[0]![0]).toBe(WORKSPACE);
  });

  test("says so when there are no conversations, and offers to start one", async () => {
    mocks.listConversations.mockResolvedValue(list([]));
    open();

    expect(await screen.findByText(/No conversations yet/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "New conversation" })).toBeEnabled();
    expect(screen.getByRole("heading", { name: "Select a conversation" })).toBeInTheDocument();
  });

  test("shows the error and retries on request", async () => {
    mocks.listConversations
      .mockRejectedValueOnce(new ApiError(0, "network_error", "Could not reach the server."))
      .mockResolvedValueOnce(list([conversation(FIRST, "Refund policy")]));
    open();

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("Could not load conversations");
    await userEvent.setup().click(within(alert).getByRole("button", { name: "Try again" }));

    expect(await screen.findByRole("link", { name: "Refund policy" })).toBeInTheDocument();
  });

  test("marks the open conversation as current", async () => {
    open(SECOND);

    const active = await screen.findByRole("link", { name: "Shipping times" });
    expect(active).toHaveAttribute("aria-current", "page");
    expect(screen.getByRole("link", { name: "Refund policy" })).not.toHaveAttribute("aria-current");
  });

  test("tells the user their conversations are private", async () => {
    open();

    expect(
      await screen.findByText(/Research · Your conversations here are private to you/),
    ).toBeInTheDocument();
  });
});

describe("creating a conversation", () => {
  test("creates it in this workspace and opens it", async () => {
    const created = conversation("44444444-4444-4444-8444-444444444444", "New conversation");
    mocks.createConversation.mockResolvedValue(created);
    open();

    await userEvent.setup().click(await screen.findByRole("button", { name: "New conversation" }));

    await vi.waitFor(() =>
      expect(mocks.push).toHaveBeenCalledWith(`/dashboard/workspaces/${WORKSPACE}/chat/${created.id}`),
    );
    expect(mocks.createConversation).toHaveBeenCalledWith(WORKSPACE);
  });

  test("shows the reason when it cannot be created", async () => {
    mocks.createConversation.mockRejectedValue(
      new ApiError(404, "workspace_not_found", "Workspace not found."),
    );
    open();

    await userEvent.setup().click(await screen.findByRole("button", { name: "New conversation" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("Workspace not found.");
    expect(mocks.push).not.toHaveBeenCalled();
  });
});

describe("opening a conversation", () => {
  test("loads its title and its message history in order", async () => {
    mocks.listMessages.mockResolvedValue(
      page([message(1, "What is the refund policy?"), message(2, "And for sale items?")]),
    );
    open(FIRST);

    expect(await screen.findByRole("heading", { name: "Refund policy", level: 2 })).toBeInTheDocument();
    const messages = within(await screen.findByRole("list", { name: "Messages" })).getAllByRole("listitem");
    expect(messages.map((item) => within(item).getByText(/policy|sale/).textContent)).toEqual([
      "What is the refund policy?",
      "And for sale items?",
    ]);
    expect(messages[0]).toHaveTextContent("You said:");
    expect(mocks.getConversation.mock.calls[0]![0]).toBe(FIRST);
    expect(mocks.listMessages.mock.calls[0]!.slice(0, 2)).toEqual([FIRST, undefined]);
  });

  test("shows loading states while the conversation and its messages load", async () => {
    mocks.getConversation.mockReturnValue(new Promise(() => {}));
    open(FIRST);

    expect(await screen.findByRole("status", { name: "Loading conversation" })).toBeInTheDocument();
  });

  test("a new conversation shows an empty state that explains what answers are based on", async () => {
    open(FIRST);

    expect(await screen.findByRole("heading", { name: "No messages yet" })).toBeInTheDocument();
    expect(screen.getByText(/Answers come only from those documents/)).toBeInTheDocument();
    expect(screen.getByText(/Only you can see this conversation/)).toBeInTheDocument();
    expect(composer()).toBeEnabled();
  });

  test("only what was stored is shown: no reply is invented", async () => {
    mocks.listMessages.mockResolvedValue(page([message(1, "What is the refund policy?")]));
    open(FIRST);

    const messages = within(await screen.findByRole("list", { name: "Messages" })).getAllByRole("listitem");
    expect(messages).toHaveLength(1);
    expect(screen.queryByText("Assistant said:")).not.toBeInTheDocument();
  });

  test("a stored assistant message would be shown as the assistant's", async () => {
    mocks.listMessages.mockResolvedValue(
      page([message(1, "Question"), message(2, "Stored answer", "assistant")]),
    );
    open(FIRST);

    const messages = within(await screen.findByRole("list", { name: "Messages" })).getAllByRole("listitem");
    expect(messages[1]).toHaveTextContent("Assistant said:");
    expect(messages[1]).toHaveTextContent("Stored answer");
  });

  test("message text is shown as text, never run as markup", async () => {
    const hostile = '<img src=x onerror="alert(1)"><script>alert(2)</script>';
    mocks.listMessages.mockResolvedValue(page([message(1, hostile)]));
    const { container } = open(FIRST);

    expect(await screen.findByText(hostile)).toBeInTheDocument();
    expect(container.querySelector("script, img")).toBeNull();
  });

  test("older messages can be loaded and appear above the newer ones", async () => {
    mocks.listMessages.mockImplementation(async (_id: string, before?: number) =>
      before === undefined
        ? page([message(3, "third"), message(4, "fourth")], true)
        : page([message(1, "first"), message(2, "second")]),
    );
    open(FIRST);

    await userEvent.setup().click(await screen.findByRole("button", { name: "Load earlier messages" }));

    await screen.findByText("first");
    const texts = within(screen.getByRole("list", { name: "Messages" }))
      .getAllByRole("listitem")
      .map((item) => item.querySelector("p")!.textContent);
    expect(texts).toEqual(["first", "second", "third", "fourth"]);
    expect(mocks.listMessages.mock.calls.at(-1)!.slice(0, 2)).toEqual([FIRST, 3]);
    expect(screen.queryByRole("button", { name: "Load earlier messages" })).not.toBeInTheDocument();
  });

  test("a conversation that is not the user's shows not found, with a way back", async () => {
    mocks.getConversation.mockRejectedValue(
      new ApiError(404, "conversation_not_found", "Conversation not found."),
    );
    open(FIRST);

    expect(await screen.findByRole("heading", { name: "Conversation not found" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Back to conversations" })).toHaveAttribute(
      "href",
      `/dashboard/workspaces/${WORKSPACE}/chat`,
    );
    expect(screen.queryByRole("textbox", { name: "Message" })).not.toBeInTheDocument();
    expect(mocks.listMessages).not.toHaveBeenCalled();
  });

  test("a server failure loading messages can be retried", async () => {
    mocks.listMessages
      .mockRejectedValueOnce(new ApiError(500, "internal_error", "Something went wrong on our side."))
      .mockResolvedValueOnce(page([message(1, "recovered")]));
    open(FIRST);

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("Could not load messages");
    await userEvent.setup().click(within(alert).getByRole("button", { name: "Try again" }));

    expect(await screen.findByText("recovered")).toBeInTheDocument();
  });
});

const REFUND_DOC = "66666666-6666-4666-8666-666666666666";
const NOTES_DOC = "77777777-7777-4777-8777-777777777777";

function citation(number: number, overrides: Partial<Citation> = {}): Citation {
  return {
    number,
    document_id: REFUND_DOC,
    document_name: "refund-policy.pdf",
    chunk_id: `88888888-8888-4888-8888-${String(number).padStart(12, "0")}`,
    page_number: 4,
    paragraph_index: null,
    line_start: null,
    line_end: null,
    excerpt: "Customers can request a refund within 30 days of purchase.",
    ...overrides,
  };
}

function answer(sequence: number, content: string, citations: Citation[] = [], grounded = true): Message {
  return {
    ...message(sequence, content, "assistant"),
    model: "gpt-4.1-mini",
    metadata: { grounded },
    citations,
  };
}

function chatResult(question: string, reply: Message, sequence = 1) {
  return { conversation_id: FIRST, user_message: message(sequence, question), assistant_message: reply };
}

describe("asking a question", () => {
  test("sends the question with an idempotency ID, then shows the stored question and answer", async () => {
    mocks.askQuestion.mockResolvedValue(
      chatResult("What is the refund policy?", answer(2, "Refunds within 30 days [1].", [citation(1)])),
    );
    open(FIRST);
    const user = userEvent.setup();

    await user.type(await screen.findByRole("textbox", { name: "Message" }), "What is the refund policy?");
    await user.click(screen.getByRole("button", { name: "Send" }));

    const list = await screen.findByRole("list", { name: "Messages" });
    expect(await within(list).findByText("What is the refund policy?")).toBeInTheDocument();
    expect(within(list).getByText(/Refunds within 30 days/)).toBeInTheDocument();
    const [conversationId, content, clientId] = mocks.askQuestion.mock.calls[0]!;
    expect([conversationId, content]).toEqual([FIRST, "What is the refund policy?"]);
    expect(clientId).toMatch(/^[0-9a-f-]{36}$/);
    expect(composer()).toHaveValue("");
  });

  test("shows the question and a progress message while the answer is written", async () => {
    let finish: (value: unknown) => void = () => {};
    mocks.askQuestion.mockReturnValue(new Promise((resolve) => (finish = resolve)));
    open(FIRST);
    const user = userEvent.setup();
    const box = await screen.findByRole("textbox", { name: "Message" });

    await user.type(box, "What is the refund policy?{Enter}");

    expect(await screen.findByText("What is the refund policy?")).toBeInTheDocument();
    expect(screen.getByRole("status")).toHaveTextContent("Searching your documents and writing an answer…");
    const sending = screen.getByRole("button", { name: "Answering…" });
    expect(sending).toBeDisabled();
    expect(sending).toHaveAttribute("aria-busy", "true");
    expect(box).toBeDisabled();
    fireEvent.keyDown(box, { key: "Enter" });
    expect(mocks.askQuestion).toHaveBeenCalledTimes(1);

    finish(chatResult("What is the refund policy?", answer(2, "Refunds within 30 days [1].", [citation(1)])));

    expect(await screen.findByText(/Refunds within 30 days/)).toBeInTheDocument();
    expect(screen.queryByText("Searching your documents and writing an answer…")).not.toBeInTheDocument();
    await vi.waitFor(() => expect(screen.getByRole("button", { name: "Send" })).toBeEnabled());
  });

  test("Enter sends and Shift+Enter makes a new line", async () => {
    mocks.askQuestion.mockResolvedValue(
      chatResult("line one\nline two", answer(2, "ok [1].", [citation(1)])),
    );
    open(FIRST);
    const user = userEvent.setup();
    const box = await screen.findByRole("textbox", { name: "Message" });

    await user.type(box, "line one{Shift>}{Enter}{/Shift}line two");
    expect(box).toHaveValue("line one\nline two");
    expect(mocks.askQuestion).not.toHaveBeenCalled();

    await user.type(box, "{Enter}");

    await vi.waitFor(() => expect(mocks.askQuestion).toHaveBeenCalledTimes(1));
    expect(mocks.askQuestion.mock.calls[0]![1]).toBe("line one\nline two");
  });

  test("Enter does not send while an input method is still composing", async () => {
    open(FIRST);
    const box = await screen.findByRole("textbox", { name: "Message" });
    await userEvent.setup().type(box, "こん");

    fireEvent.keyDown(box, { key: "Enter", isComposing: true });

    expect(mocks.askQuestion).not.toHaveBeenCalled();
  });

  test.each(["", "   ", "\n\n"])("an empty or blank question %j is not sent", async (draft) => {
    open(FIRST);
    const user = userEvent.setup();
    const box = await screen.findByRole("textbox", { name: "Message" });

    if (draft) fireEvent.change(box, { target: { value: draft } });
    await user.click(screen.getByRole("button", { name: "Send" }));

    expect(screen.getByRole("alert")).toHaveTextContent("Write a message first.");
    expect(box).toBeInvalid();
    expect(mocks.askQuestion).not.toHaveBeenCalled();
  });

  test("the box cannot hold more than the API accepts", async () => {
    open(FIRST);

    expect(await screen.findByRole("textbox", { name: "Message" })).toHaveAttribute("maxlength", "8000");
  });

  test("a follow-up is sent in the same conversation after the first answer", async () => {
    mocks.askQuestion
      .mockResolvedValueOnce(
        chatResult(
          "List the policies.",
          answer(2, "Refunds [1] and shipping [2].", [citation(1), citation(2)]),
        ),
      )
      .mockResolvedValueOnce(
        chatResult("Explain the second point.", answer(4, "Shipping takes two days [1].", [citation(1)]), 3),
      );
    open(FIRST);
    const user = userEvent.setup();
    const box = await screen.findByRole("textbox", { name: "Message" });

    await user.type(box, "List the policies.{Enter}");
    await screen.findByText(/Refunds/);
    await user.type(box, "Explain the second point.{Enter}");

    expect(await screen.findByText(/Shipping takes two days/)).toBeInTheDocument();
    expect(mocks.askQuestion.mock.calls.map((call) => call.slice(0, 2))).toEqual([
      [FIRST, "List the policies."],
      [FIRST, "Explain the second point."],
    ]);
    expect(mocks.askQuestion.mock.calls[0]![2]).not.toBe(mocks.askQuestion.mock.calls[1]![2]);
  });

  test("the answer survives a refresh, because it comes from the stored history", async () => {
    mocks.listMessages.mockResolvedValue(
      page([
        message(1, "What is the refund policy?"),
        answer(2, "Refunds within 30 days [1].", [citation(1)]),
      ]),
    );
    open(FIRST);

    const list = await screen.findByRole("list", { name: "Messages" });
    expect(within(list).getByText(/Refunds within 30 days/)).toBeInTheDocument();
    expect(within(list).getByRole("button", { name: "Show source 1" })).toBeInTheDocument();
    expect(within(list).getByText("refund-policy.pdf")).toBeInTheDocument();
  });

  test("asking refreshes the list and the heading, so an automatic title appears", async () => {
    mocks.listConversations
      .mockResolvedValueOnce(list([conversation(FIRST, "New conversation")]))
      .mockResolvedValue(list([conversation(FIRST, "What is the refund policy?")]));
    mocks.getConversation
      .mockResolvedValueOnce(conversation(FIRST, "New conversation"))
      .mockResolvedValue(conversation(FIRST, "What is the refund policy?"));
    mocks.askQuestion.mockResolvedValue(
      chatResult("What is the refund policy?", answer(2, "ok [1].", [citation(1)])),
    );
    open(FIRST);
    const user = userEvent.setup();

    await screen.findByRole("heading", { name: "New conversation", level: 2 });
    await user.type(
      await screen.findByRole("textbox", { name: "Message" }),
      "What is the refund policy?{Enter}",
    );

    expect(
      await screen.findByRole("heading", { name: "What is the refund policy?", level: 2 }),
    ).toBeInTheDocument();
    expect(await screen.findByRole("link", { name: "What is the refund policy?" })).toBeInTheDocument();
  });

  test("an archived conversation cannot be written to", async () => {
    mocks.getConversation.mockResolvedValue({ ...conversation(FIRST, "Old"), status: "archived" });
    open(FIRST);

    expect(await screen.findByRole("textbox", { name: "Message" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Send" })).toBeDisabled();
  });
});

describe("when answering fails", () => {
  test("a stored question offers Try again, which reuses its ID so it is not stored twice", async () => {
    mocks.askQuestion
      .mockRejectedValueOnce(
        new ApiError(
          503,
          "ai_unavailable",
          "The AI service is busy or unreachable. Your question was saved; try again shortly.",
        ),
      )
      .mockResolvedValueOnce(chatResult("important question", answer(2, "Here it is [1].", [citation(1)])));
    mocks.listMessages
      .mockResolvedValueOnce(page([]))
      .mockResolvedValue(page([message(1, "important question")]));
    open(FIRST);
    const user = userEvent.setup();
    const box = await screen.findByRole("textbox", { name: "Message" });

    await user.type(box, "important question{Enter}");

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("Your question was saved");
    // The question is in the conversation already, so the box is free for something else.
    expect(box).toHaveValue("");
    expect(
      await within(screen.getByRole("list", { name: "Messages" })).findByText("important question"),
    ).toBeInTheDocument();
    expect(screen.queryByText("Assistant said:")).not.toBeInTheDocument();

    await user.click(within(alert).getByRole("button", { name: "Try again" }));

    expect(await screen.findByText(/Here it is/)).toBeInTheDocument();
    expect(mocks.askQuestion.mock.calls[1]!.slice(1)).toEqual(mocks.askQuestion.mock.calls[0]!.slice(1));
  });

  test.each([
    ["ai_misconfigured", 503, "Answering is not available right now. Your question was saved."],
    [
      "ai_invalid_response",
      502,
      "The AI service returned an unusable answer. Your question was saved; try again.",
    ],
    ["embeddings_unavailable", 503, "Search is temporarily unavailable. Try again."],
  ])("%s is explained, with a retry", async (code, status, message) => {
    mocks.askQuestion.mockRejectedValue(new ApiError(status, code, message));
    open(FIRST);

    await userEvent.setup().type(await screen.findByRole("textbox", { name: "Message" }), "question{Enter}");

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(message);
    expect(within(alert).getByRole("button", { name: "Try again" })).toBeInTheDocument();
  });

  test("a lost connection keeps the question retryable with the same ID", async () => {
    mocks.askQuestion
      .mockRejectedValueOnce(new ApiError(0, "network_error", "Could not reach the server."))
      .mockResolvedValueOnce(chatResult("question", answer(2, "ok [1].", [citation(1)])));
    open(FIRST);
    const user = userEvent.setup();

    await user.type(await screen.findByRole("textbox", { name: "Message" }), "question{Enter}");
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("Could not reach the server.");
    await user.click(within(alert).getByRole("button", { name: "Try again" }));

    await vi.waitFor(() => expect(mocks.askQuestion).toHaveBeenCalledTimes(2));
    expect(mocks.askQuestion.mock.calls[1]![2]).toBe(mocks.askQuestion.mock.calls[0]![2]);
  });

  test.each([
    [new ApiError(409, "conversation_busy", "x"), "An answer is still being written. Wait for it to finish."],
    [new ApiError(422, "validation_error", "x"), "That question could not be sent."],
    [
      new ApiError(503, "ai_not_configured", "x"),
      "Answering questions has not been set up on the server yet.",
    ],
  ])("a question that was not stored goes back into the box", async (error, expected) => {
    mocks.askQuestion.mockRejectedValue(error);
    open(FIRST);
    const box = await screen.findByRole("textbox", { name: "Message" });

    await userEvent.setup().type(box, "unsent thought{Enter}");

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(expected);
    expect(within(alert).queryByRole("button", { name: "Try again" })).not.toBeInTheDocument();
    expect(box).toHaveValue("unsent thought");
  });

  test("resending the returned text reuses its ID", async () => {
    mocks.askQuestion
      .mockRejectedValueOnce(new ApiError(409, "conversation_busy", "x"))
      .mockResolvedValueOnce(chatResult("unsent thought", answer(2, "ok [1].", [citation(1)])));
    open(FIRST);
    const user = userEvent.setup();
    const box = await screen.findByRole("textbox", { name: "Message" });

    await user.type(box, "unsent thought{Enter}");
    await screen.findByRole("alert");
    await user.click(screen.getByRole("button", { name: "Send" }));

    await vi.waitFor(() => expect(mocks.askQuestion).toHaveBeenCalledTimes(2));
    expect(mocks.askQuestion.mock.calls[1]![2]).toBe(mocks.askQuestion.mock.calls[0]![2]);
  });

  test("the server's raw text is not shown for an unexpected error", async () => {
    mocks.askQuestion.mockRejectedValue(
      new ApiError(500, "internal_error", "psycopg.OperationalError at db:5432"),
    );
    open(FIRST);

    await userEvent.setup().type(await screen.findByRole("textbox", { name: "Message" }), "question{Enter}");

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("Something went wrong.");
    expect(alert).not.toHaveTextContent("psycopg");
  });
});

describe("answers and their sources", () => {
  async function showAnswer(reply: Message) {
    mocks.listMessages.mockResolvedValue(page([message(1, "Question?"), reply]));
    open(FIRST);
    return screen.findByRole("list", { name: "Messages" });
  }

  test("each source is listed with its document name and page", async () => {
    const list = await showAnswer(
      answer(2, "Refunds take 30 days [1]. Shipping is quick [2].", [
        citation(1),
        citation(2, { document_id: NOTES_DOC, document_name: "shipping.pdf", page_number: 2 }),
      ]),
    );

    const sources = within(within(list).getByRole("region", { name: "Sources" })).getAllByRole("listitem");
    expect(sources).toHaveLength(2);
    expect(sources[0]).toHaveTextContent("refund-policy.pdf");
    expect(sources[0]).toHaveTextContent("Page 4");
    expect(sources[1]).toHaveTextContent("shipping.pdf");
    expect(sources[1]).toHaveTextContent("Page 2");
  });

  test.each([
    [{ page_number: 4 }, "Page 4"],
    [{ page_number: null, paragraph_index: 12 }, "Paragraph 13"],
    [{ page_number: null, line_start: 10, line_end: 24 }, "Lines 10–24"],
  ])("a source shows the reference its format has", async (overrides, label) => {
    const list = await showAnswer(answer(2, "Answer [1].", [citation(1, overrides)]));

    const source = within(within(list).getByRole("region", { name: "Sources" })).getByRole("listitem");
    expect(source).toHaveTextContent(label);
    if (!("page_number" in overrides) || overrides.page_number === null) {
      expect(source).not.toHaveTextContent(/Page \d/);
    }
  });

  test("the excerpt can be opened from the source card or from the marker in the answer", async () => {
    const list = await showAnswer(answer(2, "Refunds take 30 days [1].", [citation(1)]));
    const user = userEvent.setup();
    const excerpt = within(list).getByText("Customers can request a refund within 30 days of purchase.");

    expect(excerpt).not.toBeVisible();
    await user.click(within(list).getByRole("button", { name: "Show excerpt" }));
    expect(excerpt).toBeVisible();
    await user.click(within(list).getByRole("button", { name: "Hide excerpt" }));
    expect(excerpt).not.toBeVisible();

    await user.click(within(list).getByRole("button", { name: "Show source 1" }));
    expect(excerpt).toBeVisible();
    expect(within(list).getByRole("button", { name: "Hide excerpt" })).toHaveAttribute(
      "aria-expanded",
      "true",
    );
  });

  test("markers for several sources are each clickable", async () => {
    const list = await showAnswer(
      answer(2, "Both say so [1, 2].", [citation(1), citation(2, { document_name: "b.pdf" })]),
    );

    expect(within(list).getByRole("button", { name: "Show source 1" })).toBeInTheDocument();
    expect(within(list).getByRole("button", { name: "Show source 2" })).toBeInTheDocument();
    expect(within(list).getByText(/Both say so/)).toBeInTheDocument();
  });

  test("a marker with no matching source is shown as plain text", async () => {
    const list = await showAnswer(answer(2, "Odd [3].", [citation(1)]));

    expect(within(list).queryByRole("button", { name: "Show source 3" })).not.toBeInTheDocument();
    expect(within(list).getAllByRole("listitem")[1]).toHaveTextContent("Odd [3].");
  });

  test("a source document can be downloaded through a signed link", async () => {
    mocks.getDownload.mockResolvedValue({
      url: "https://storage.test/signed?token=abc",
      expires_in: 60,
      filename: "refund-policy.pdf",
    });
    const list = await showAnswer(answer(2, "Refunds [1].", [citation(1)]));

    await userEvent.setup().click(within(list).getByRole("button", { name: "Download refund-policy.pdf" }));

    await vi.waitFor(() =>
      expect(mocks.startDownload).toHaveBeenCalledWith("https://storage.test/signed?token=abc"),
    );
    expect(mocks.getDownload).toHaveBeenCalledWith(REFUND_DOC);
  });

  test("an answer the documents could not support is marked and has no sources", async () => {
    const list = await showAnswer(
      answer(
        2,
        "I couldn't find sufficient information in your uploaded documents to answer that question.",
        [],
        false,
      ),
    );

    expect(within(list).getByText(/couldn't find sufficient information/)).toBeInTheDocument();
    expect(within(list).getByText(/No supporting passage was found/)).toBeInTheDocument();
    expect(within(list).queryByRole("region", { name: "Sources" })).not.toBeInTheDocument();
  });

  test("source excerpts and answers are shown as text, never run as markup", async () => {
    const hostile = '<img src=x onerror="alert(1)"><script>alert(2)</script>';
    const { container } = (() => {
      mocks.listMessages.mockResolvedValue(
        page([message(1, "Q"), answer(2, `${hostile} [1]`, [citation(1, { excerpt: hostile })])]),
      );
      return open(FIRST);
    })();

    await screen.findByRole("list", { name: "Messages" });
    expect(container.querySelector("script, img")).toBeNull();
  });
});

describe("renaming", () => {
  async function openRename(user: ReturnType<typeof userEvent.setup>) {
    await user.click(await screen.findByRole("button", { name: "Actions for Refund policy" }));
    await user.click(await screen.findByRole("menuitem", { name: "Rename" }));
    return screen.findByRole("dialog", { name: "Rename conversation" });
  }

  test("updates the sidebar and the page heading", async () => {
    mocks.renameConversation.mockResolvedValue(conversation(FIRST, "Refunds and returns"));
    open(FIRST);
    const user = userEvent.setup();
    await screen.findByRole("heading", { name: "Refund policy", level: 2 });
    const dialog = await openRename(user);
    mocks.listConversations.mockResolvedValue(
      list([conversation(FIRST, "Refunds and returns"), conversation(SECOND, "Shipping times")]),
    );

    const field = within(dialog).getByLabelText("Title");
    expect(field).toHaveValue("Refund policy");
    await user.clear(field);
    await user.type(field, "  Refunds and returns  ");
    await user.click(within(dialog).getByRole("button", { name: "Save" }));

    await vi.waitFor(() =>
      expect(mocks.renameConversation).toHaveBeenCalledWith(FIRST, "Refunds and returns"),
    );
    expect(await screen.findByRole("heading", { name: "Refunds and returns", level: 2 })).toBeInTheDocument();
    expect(await screen.findByRole("link", { name: "Refunds and returns" })).toBeInTheDocument();
    await vi.waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
  });

  test("an empty title is refused before any request", async () => {
    open(FIRST);
    const user = userEvent.setup();
    const dialog = await openRename(user);

    await user.clear(within(dialog).getByLabelText("Title"));
    await user.click(within(dialog).getByRole("button", { name: "Save" }));

    expect(within(dialog).getByLabelText("Title")).toHaveAccessibleDescription("Enter a title.");
    expect(mocks.renameConversation).not.toHaveBeenCalled();
  });

  test("shows the reason when the rename fails, and stays open", async () => {
    mocks.renameConversation.mockRejectedValue(
      new ApiError(404, "conversation_not_found", "Conversation not found."),
    );
    open(FIRST);
    const user = userEvent.setup();
    const dialog = await openRename(user);

    await user.click(within(dialog).getByRole("button", { name: "Save" }));

    expect(await within(dialog).findByRole("alert")).toHaveTextContent("Conversation not found.");
  });
});

describe("deleting", () => {
  async function openDelete(user: ReturnType<typeof userEvent.setup>, title: string) {
    await user.click(await screen.findByRole("button", { name: `Actions for ${title}` }));
    await user.click(await screen.findByRole("menuitem", { name: "Delete" }));
    return screen.findByRole("dialog", { name: `Delete “${title}”?` });
  }

  test("asks first, and deletes only after confirming", async () => {
    mocks.deleteConversation.mockResolvedValue(undefined);
    open(SECOND);
    const user = userEvent.setup();

    const dialog = await openDelete(user, "Refund policy");
    expect(dialog).toHaveTextContent("all of its messages are removed");
    expect(mocks.deleteConversation).not.toHaveBeenCalled();

    await user.click(within(dialog).getByRole("button", { name: "Delete conversation" }));

    await vi.waitFor(() => expect(mocks.deleteConversation).toHaveBeenCalledWith(FIRST));
    // A conversation that was not open: stay where we are.
    expect(mocks.replace).not.toHaveBeenCalled();
  });

  test("cancelling keeps the conversation", async () => {
    open(FIRST);
    const user = userEvent.setup();

    const dialog = await openDelete(user, "Refund policy");
    await user.click(within(dialog).getByRole("button", { name: "Cancel" }));

    await vi.waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    expect(mocks.deleteConversation).not.toHaveBeenCalled();
  });

  test("deleting the open conversation moves to another one", async () => {
    mocks.deleteConversation.mockResolvedValue(undefined);
    open(FIRST);
    const user = userEvent.setup();

    const dialog = await openDelete(user, "Refund policy");
    await user.click(within(dialog).getByRole("button", { name: "Delete conversation" }));

    await vi.waitFor(() =>
      expect(mocks.replace).toHaveBeenCalledWith(`/dashboard/workspaces/${WORKSPACE}/chat/${SECOND}`),
    );
  });

  test("deleting the last conversation returns to the empty state", async () => {
    mocks.listConversations.mockResolvedValue(list([conversation(FIRST, "Refund policy")]));
    mocks.deleteConversation.mockResolvedValue(undefined);
    open(FIRST);
    const user = userEvent.setup();

    const dialog = await openDelete(user, "Refund policy");
    await user.click(within(dialog).getByRole("button", { name: "Delete conversation" }));

    await vi.waitFor(() =>
      expect(mocks.replace).toHaveBeenCalledWith(`/dashboard/workspaces/${WORKSPACE}/chat`),
    );
  });

  test("shows the reason when deleting fails, and stays open", async () => {
    mocks.deleteConversation.mockRejectedValue(
      new ApiError(500, "internal_error", "Something went wrong on our side."),
    );
    open(FIRST);
    const user = userEvent.setup();

    const dialog = await openDelete(user, "Refund policy");
    await user.click(within(dialog).getByRole("button", { name: "Delete conversation" }));

    expect(await within(dialog).findByRole("alert")).toHaveTextContent("Something went wrong on our side.");
    expect(mocks.replace).not.toHaveBeenCalled();
  });
});

describe("workspace switching", () => {
  test("every request names only the workspace being viewed", async () => {
    open(FIRST);
    await screen.findByRole("heading", { name: "Refund policy", level: 2 });

    expect(mocks.getWorkspace.mock.calls.every((call) => call[0] === WORKSPACE)).toBe(true);
    expect(mocks.listConversations.mock.calls.every((call) => call[0] === WORKSPACE)).toBe(true);
  });

  test("switching workspace shows only the new workspace's conversations", async () => {
    const view = open(FIRST);
    await screen.findByRole("link", { name: "Refund policy" });
    mocks.listMessages.mockResolvedValue(page([message(1, "Alice's confidential question")]));

    mocks.params = { workspaceId: OTHER_WORKSPACE };
    mocks.getWorkspace.mockResolvedValue({
      id: OTHER_WORKSPACE,
      name: "Other team",
      owner_id: "33333333-3333-4333-8333-333333333333",
      role: "member",
      created_at: "2026-11-11T00:00:00Z",
      updated_at: "2026-11-11T00:00:00Z",
    });
    mocks.listConversations.mockResolvedValue(
      list([conversation(SECOND, "Other team's topic", OTHER_WORKSPACE)]),
    );
    view.rerender(
      <QueryClientProvider client={view.queryClient}>
        <ChatWorkspace />
      </QueryClientProvider>,
    );

    expect(await screen.findByRole("link", { name: "Other team's topic" })).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "Refund policy" })).not.toBeInTheDocument();
    expect(screen.queryByText("Alice's confidential question")).not.toBeInTheDocument();
    expect(mocks.listConversations).toHaveBeenLastCalledWith(OTHER_WORKSPACE, expect.anything());
  });

  test("a conversation opened under the wrong workspace's URL is not shown", async () => {
    mocks.getConversation.mockResolvedValue(conversation(FIRST, "Belongs elsewhere", OTHER_WORKSPACE));
    open(FIRST);

    expect(await screen.findByRole("heading", { name: "Conversation not found" })).toBeInTheDocument();
    expect(screen.queryByText("Belongs elsewhere")).not.toBeInTheDocument();
    expect(mocks.listMessages).not.toHaveBeenCalled();
  });

  test("a workspace the user is not in shows not found and loads no conversations into view", async () => {
    mocks.getWorkspace.mockRejectedValue(new ApiError(404, "workspace_not_found", "Workspace not found."));
    mocks.listConversations.mockRejectedValue(
      new ApiError(404, "workspace_not_found", "Workspace not found."),
    );
    open();

    expect(await screen.findByRole("heading", { name: "Workspace not found" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "New conversation" })).not.toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Back to workspaces" })).toHaveAttribute(
      "href",
      "/dashboard/workspaces",
    );
  });
});

describe("authentication failure", () => {
  test("an expired session while loading is reported, with nothing from the conversation shown", async () => {
    const expired = new ApiError(401, "token_expired", "The access token has expired.");
    mocks.listConversations.mockRejectedValue(expired);
    mocks.getConversation.mockRejectedValue(expired);
    open(FIRST);

    expect(await screen.findByText("Could not load conversations")).toBeInTheDocument();
    expect(screen.getAllByText("The access token has expired.").length).toBeGreaterThan(0);
    expect(screen.queryByRole("textbox", { name: "Message" })).not.toBeInTheDocument();
  });

  test("an expired session while sending keeps the draft and says why", async () => {
    mocks.askQuestion.mockRejectedValue(new ApiError(401, "token_expired", "The access token has expired."));
    open(FIRST);
    const box = await screen.findByRole("textbox", { name: "Message" });

    await userEvent.setup().type(box, "unsent thought{Enter}");

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Your session has expired. Sign in again to continue.",
    );
    expect(box).toHaveValue("unsent thought");
  });
});

describe("responsive navigation", () => {
  test("with no conversation open, the list is shown on small screens", async () => {
    open();

    const sidebar = await screen.findByRole("complementary", { name: "Conversations" });
    expect(sidebar).toHaveClass("flex");
    expect(sidebar).not.toHaveClass("hidden");
  });

  test("with a conversation open, the list gives way to the chat on small screens and returns on wide ones", async () => {
    open(FIRST);

    const sidebar = await screen.findByRole("complementary", { name: "Conversations" });
    expect(sidebar).toHaveClass("hidden", "md:flex");
  });

  test("an open conversation has a way back to the list for small screens", async () => {
    open(FIRST);

    const back = await screen.findByRole("link", { name: "All conversations" });
    expect(back).toHaveAttribute("href", `/dashboard/workspaces/${WORKSPACE}/chat`);
    expect(back).toHaveClass("md:hidden");
  });

  test("there is always a way back to the workspace", async () => {
    open(FIRST);

    expect(await screen.findByRole("link", { name: "Back to workspace" })).toHaveAttribute(
      "href",
      `/dashboard/workspaces/${WORKSPACE}`,
    );
  });
});
