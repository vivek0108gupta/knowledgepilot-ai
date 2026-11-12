import { QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, test, vi } from "vitest";

import { ApiError } from "@/lib/api";
import type { Conversation, Message, MessagePage } from "@/lib/conversations";
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
  sendMessage: vi.fn(),
}));

vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: mocks.push, replace: mocks.replace }),
  useParams: () => mocks.params,
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
  sendMessage: mocks.sendMessage,
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

  test("a new conversation shows an empty state that is honest about AI answers", async () => {
    open(FIRST);

    expect(await screen.findByRole("heading", { name: "No messages yet" })).toBeInTheDocument();
    expect(
      screen.getByText(/AI answers are not available yet; they arrive in the next milestone/),
    ).toBeInTheDocument();
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

describe("sending a message", () => {
  test("sends the text with an idempotency ID, then shows what the server stored", async () => {
    const stored = message(1, "What is the refund policy?");
    mocks.sendMessage.mockResolvedValue(stored);
    mocks.listMessages.mockResolvedValueOnce(page([])).mockResolvedValue(page([stored]));
    open(FIRST);
    const user = userEvent.setup();

    await user.type(await screen.findByRole("textbox", { name: "Message" }), "What is the refund policy?");
    await user.click(screen.getByRole("button", { name: "Send" }));

    expect(
      await within(await screen.findByRole("list", { name: "Messages" })).findByText(
        "What is the refund policy?",
      ),
    ).toBeInTheDocument();
    const [conversationId, content, clientId] = mocks.sendMessage.mock.calls[0]!;
    expect([conversationId, content]).toEqual([FIRST, "What is the refund policy?"]);
    expect(clientId).toMatch(/^[0-9a-f-]{36}$/);
    await vi.waitFor(() => expect(composer()).toHaveValue(""));
  });

  test("Enter sends and Shift+Enter makes a new line", async () => {
    mocks.sendMessage.mockResolvedValue(message(1, "line one\nline two"));
    open(FIRST);
    const user = userEvent.setup();
    const box = await screen.findByRole("textbox", { name: "Message" });

    await user.type(box, "line one{Shift>}{Enter}{/Shift}line two");
    expect(box).toHaveValue("line one\nline two");
    expect(mocks.sendMessage).not.toHaveBeenCalled();

    await user.type(box, "{Enter}");

    await vi.waitFor(() => expect(mocks.sendMessage).toHaveBeenCalledTimes(1));
    expect(mocks.sendMessage.mock.calls[0]![1]).toBe("line one\nline two");
  });

  test("Enter does not send while an input method is still composing", async () => {
    open(FIRST);
    const box = await screen.findByRole("textbox", { name: "Message" });
    await userEvent.setup().type(box, "こん");

    fireEvent.keyDown(box, { key: "Enter", isComposing: true });

    expect(mocks.sendMessage).not.toHaveBeenCalled();
  });

  test.each(["", "   ", "\n\n"])("an empty or blank message %j is not sent", async (draft) => {
    open(FIRST);
    const user = userEvent.setup();
    const box = await screen.findByRole("textbox", { name: "Message" });

    if (draft) fireEvent.change(box, { target: { value: draft } });
    await user.click(screen.getByRole("button", { name: "Send" }));

    expect(screen.getByRole("alert")).toHaveTextContent("Write a message first.");
    expect(box).toBeInvalid();
    expect(mocks.sendMessage).not.toHaveBeenCalled();
  });

  test("the box cannot hold more than the API accepts", async () => {
    open(FIRST);

    expect(await screen.findByRole("textbox", { name: "Message" })).toHaveAttribute("maxlength", "8000");
  });

  test("is disabled while sending, and a second submit is ignored", async () => {
    let finish: (value: Message) => void = () => {};
    mocks.sendMessage.mockReturnValue(new Promise((resolve) => (finish = resolve)));
    open(FIRST);
    const user = userEvent.setup();
    const box = await screen.findByRole("textbox", { name: "Message" });

    await user.type(box, "hello{Enter}");

    const sending = await screen.findByRole("button", { name: "Sending…" });
    expect(sending).toBeDisabled();
    expect(sending).toHaveAttribute("aria-busy", "true");
    expect(box).toBeDisabled();
    fireEvent.keyDown(box, { key: "Enter" });
    expect(mocks.sendMessage).toHaveBeenCalledTimes(1);

    finish(message(1, "hello"));
    await vi.waitFor(() => expect(screen.getByRole("button", { name: "Send" })).toBeEnabled());
  });

  test("a failed send keeps the text, explains, and retries with the same idempotency ID", async () => {
    mocks.sendMessage
      .mockRejectedValueOnce(new ApiError(0, "network_error", "Could not reach the server."))
      .mockResolvedValueOnce(message(1, "important question"));
    open(FIRST);
    const user = userEvent.setup();
    const box = await screen.findByRole("textbox", { name: "Message" });

    await user.type(box, "important question{Enter}");

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("Could not reach the server.");
    expect(alert).toHaveTextContent("Your message is still in the box");
    expect(box).toHaveValue("important question");
    expect(box).toBeEnabled();

    await user.click(screen.getByRole("button", { name: "Send" }));

    await vi.waitFor(() => expect(mocks.sendMessage).toHaveBeenCalledTimes(2));
    // Same ID both times: if the first attempt did reach the server, it is not stored twice.
    expect(mocks.sendMessage.mock.calls[1]![2]).toBe(mocks.sendMessage.mock.calls[0]![2]);
  });

  test("a different message gets a different idempotency ID", async () => {
    mocks.sendMessage.mockImplementation(async (_id: string, content: string) => message(1, content));
    open(FIRST);
    const user = userEvent.setup();
    const box = await screen.findByRole("textbox", { name: "Message" });

    await user.type(box, "first{Enter}");
    await vi.waitFor(() => expect(box).toHaveValue(""));
    await user.type(box, "second{Enter}");

    await vi.waitFor(() => expect(mocks.sendMessage).toHaveBeenCalledTimes(2));
    expect(mocks.sendMessage.mock.calls[0]![2]).not.toBe(mocks.sendMessage.mock.calls[1]![2]);
  });

  test("sending refreshes the list and the heading, so an automatic title appears", async () => {
    mocks.listConversations
      .mockResolvedValueOnce(list([conversation(FIRST, "New conversation")]))
      .mockResolvedValue(list([conversation(FIRST, "What is the refund policy?")]));
    mocks.getConversation
      .mockResolvedValueOnce(conversation(FIRST, "New conversation"))
      .mockResolvedValue(conversation(FIRST, "What is the refund policy?"));
    mocks.sendMessage.mockResolvedValue(message(1, "What is the refund policy?"));
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
    mocks.sendMessage.mockRejectedValue(new ApiError(401, "token_expired", "The access token has expired."));
    open(FIRST);
    const box = await screen.findByRole("textbox", { name: "Message" });

    await userEvent.setup().type(box, "unsent thought{Enter}");

    expect(await screen.findByRole("alert")).toHaveTextContent("The access token has expired.");
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
