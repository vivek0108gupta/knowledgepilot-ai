"use client";

import { Menu } from "@base-ui/react/menu";
import { useInfiniteQuery, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import Link from "next/link";
import { useParams, useRouter } from "next/navigation";
import { useEffect, useId, useRef, useState, type FormEvent, type KeyboardEvent } from "react";

import { Button, buttonClass } from "@/components/ui/button";
import { Alert, EmptyState, ErrorState, LoadingRegion, Skeleton } from "@/components/ui/feedback";
import { TextField } from "@/components/ui/field";
import { Modal } from "@/components/ui/modal";
import { ApiError, errorMessage } from "@/lib/api";
import { cn } from "@/lib/cn";
import {
  chatPath,
  conversationKeys,
  conversationTitleSchema,
  createConversation,
  deleteConversation,
  getConversation,
  listConversations,
  listMessages,
  MAX_MESSAGE_LENGTH,
  MAX_TITLE_LENGTH,
  renameConversation,
  sendMessage,
  validateMessage,
  type Conversation,
  type Message,
} from "@/lib/conversations";
import { getWorkspace, workspaceKeys } from "@/lib/workspaces";

const timeFormat = new Intl.DateTimeFormat(undefined, { dateStyle: "medium", timeStyle: "short" });

const MENU_POPUP =
  "z-50 min-w-40 rounded-xl border border-zinc-200 bg-white p-1 shadow-lg outline-hidden " +
  "origin-[var(--transform-origin)] transition-[opacity,scale] duration-100 " +
  "data-ending-style:scale-95 data-ending-style:opacity-0 data-starting-style:scale-95 data-starting-style:opacity-0";
const MENU_ITEM =
  "flex cursor-default items-center rounded-lg px-3 py-2 text-sm text-zinc-800 outline-hidden select-none data-highlighted:bg-zinc-100";

function RenameDialog({
  conversation,
  workspaceId,
  onClose,
}: {
  conversation: Conversation;
  workspaceId: string;
  onClose: () => void;
}) {
  const queryClient = useQueryClient();
  const [titleError, setTitleError] = useState<string | undefined>();

  const rename = useMutation({
    mutationFn: (title: string) => renameConversation(conversation.id, title),
    onSuccess: async (updated) => {
      // The sidebar and the heading read the same two cache entries, so both update.
      queryClient.setQueryData(conversationKeys.detail(workspaceId, conversation.id), updated);
      await queryClient.invalidateQueries({ queryKey: conversationKeys.list(workspaceId), exact: true });
      onClose();
    },
  });

  function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const parsed = conversationTitleSchema.safeParse(new FormData(event.currentTarget).get("title"));
    if (!parsed.success) {
      setTitleError(parsed.error.issues[0]?.message);
      return;
    }
    setTitleError(undefined);
    rename.mutate(parsed.data);
  }

  return (
    <Modal open onOpenChange={(open) => !open && !rename.isPending && onClose()} title="Rename conversation">
      <form onSubmit={handleSubmit} noValidate className="flex flex-col gap-4">
        {rename.isError && <Alert>{errorMessage(rename.error)}</Alert>}
        <TextField
          label="Title"
          name="title"
          defaultValue={conversation.title}
          maxLength={MAX_TITLE_LENGTH}
          autoComplete="off"
          error={titleError}
          disabled={rename.isPending}
        />
        <div className="flex justify-end gap-2">
          <Button variant="secondary" onClick={onClose} disabled={rename.isPending}>
            Cancel
          </Button>
          <Button type="submit" loading={rename.isPending}>
            {rename.isPending ? "Saving…" : "Save"}
          </Button>
        </div>
      </form>
    </Modal>
  );
}

function DeleteDialog({
  conversation,
  workspaceId,
  onClose,
  onDeleted,
}: {
  conversation: Conversation;
  workspaceId: string;
  onClose: () => void;
  onDeleted: (conversation: Conversation) => void;
}) {
  const queryClient = useQueryClient();
  const remove = useMutation({
    mutationFn: () => deleteConversation(conversation.id),
    onSuccess: async () => {
      queryClient.removeQueries({ queryKey: conversationKeys.detail(workspaceId, conversation.id) });
      onDeleted(conversation);
      await queryClient.invalidateQueries({ queryKey: conversationKeys.list(workspaceId), exact: true });
    },
  });

  return (
    <Modal
      open
      onOpenChange={(open) => !open && !remove.isPending && onClose()}
      title={`Delete “${conversation.title}”?`}
      description="The conversation and all of its messages are removed. It cannot be undone."
    >
      {remove.isError && <Alert>{errorMessage(remove.error)}</Alert>}
      <div className="flex justify-end gap-2">
        <Button variant="secondary" onClick={onClose} disabled={remove.isPending}>
          Cancel
        </Button>
        <Button variant="danger" loading={remove.isPending} onClick={() => remove.mutate()}>
          {remove.isPending ? "Deleting…" : "Delete conversation"}
        </Button>
      </div>
    </Modal>
  );
}

interface SidebarProps {
  workspaceId: string;
  activeId: string | undefined;
  /** On small screens the list and the chat take turns; this hides the list while chatting. */
  hiddenOnMobile: boolean;
}

function ConversationSidebar({ workspaceId, activeId, hiddenOnMobile }: SidebarProps) {
  const router = useRouter();
  const queryClient = useQueryClient();
  const [renaming, setRenaming] = useState<Conversation | null>(null);
  const [deleting, setDeleting] = useState<Conversation | null>(null);

  const conversations = useQuery({
    queryKey: conversationKeys.list(workspaceId),
    queryFn: ({ signal }) => listConversations(workspaceId, signal),
  });

  const create = useMutation({
    mutationFn: () => createConversation(workspaceId),
    onSuccess: async (conversation) => {
      queryClient.setQueryData(conversationKeys.detail(workspaceId, conversation.id), conversation);
      await queryClient.invalidateQueries({ queryKey: conversationKeys.list(workspaceId), exact: true });
      router.push(chatPath(workspaceId, conversation.id));
    },
  });

  function handleDeleted(deleted: Conversation) {
    setDeleting(null);
    if (deleted.id !== activeId) return;
    // The open conversation is gone: move to another one, or back to the empty state.
    const next = conversations.data?.items.find((item) => item.id !== deleted.id);
    router.replace(chatPath(workspaceId, next?.id));
  }

  const items = conversations.data?.items;

  return (
    <aside
      aria-label="Conversations"
      className={cn(
        "w-full shrink-0 flex-col border-zinc-200 md:flex md:w-72 md:border-r",
        hiddenOnMobile ? "hidden" : "flex",
      )}
    >
      <div className="border-b border-zinc-200 p-3">
        <Button className="w-full" loading={create.isPending} onClick={() => create.mutate()}>
          {create.isPending ? "Creating…" : "New conversation"}
        </Button>
        {create.isError && (
          <div className="mt-2">
            <Alert>{errorMessage(create.error)}</Alert>
          </div>
        )}
      </div>

      <div className="min-h-0 flex-1 overflow-y-auto p-2">
        {conversations.isPending ? (
          <LoadingRegion label="Loading conversations">
            <div className="flex flex-col gap-2">
              {[0, 1, 2].map((index) => (
                <Skeleton key={index} className="h-10" />
              ))}
            </div>
          </LoadingRegion>
        ) : conversations.isError ? (
          <ErrorState
            title="Could not load conversations"
            description={errorMessage(conversations.error)}
            onRetry={() => void conversations.refetch()}
          />
        ) : items && items.length === 0 ? (
          <p className="px-3 py-6 text-center text-sm text-zinc-500">
            No conversations yet. Start one with the button above.
          </p>
        ) : (
          <ul className="flex flex-col gap-1">
            {items?.map((conversation) => {
              const active = conversation.id === activeId;
              return (
                <li
                  key={conversation.id}
                  className={cn(
                    "group flex items-center gap-1 rounded-lg pr-1",
                    active ? "bg-zinc-100" : "hover:bg-zinc-50",
                  )}
                >
                  <Link
                    href={chatPath(workspaceId, conversation.id)}
                    aria-current={active ? "page" : undefined}
                    className={cn(
                      "min-w-0 flex-1 truncate rounded-lg px-3 py-2 text-sm",
                      "focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-zinc-900",
                      active ? "font-medium text-zinc-900" : "text-zinc-700",
                    )}
                  >
                    {conversation.title}
                  </Link>
                  <Menu.Root>
                    <Menu.Trigger
                      aria-label={`Actions for ${conversation.title}`}
                      className="grid size-8 shrink-0 place-items-center rounded-md text-zinc-500 hover:bg-zinc-200 hover:text-zinc-900 focus-visible:outline-2 focus-visible:outline-offset-1 focus-visible:outline-zinc-900"
                    >
                      <svg aria-hidden="true" viewBox="0 0 16 16" className="size-4" fill="currentColor">
                        <circle cx="3" cy="8" r="1.4" />
                        <circle cx="8" cy="8" r="1.4" />
                        <circle cx="13" cy="8" r="1.4" />
                      </svg>
                    </Menu.Trigger>
                    <Menu.Portal>
                      <Menu.Positioner sideOffset={4} align="end" className="z-50 outline-hidden">
                        <Menu.Popup className={MENU_POPUP}>
                          <Menu.Item className={MENU_ITEM} onClick={() => setRenaming(conversation)}>
                            Rename
                          </Menu.Item>
                          <Menu.Item
                            className={cn(MENU_ITEM, "text-red-700")}
                            onClick={() => setDeleting(conversation)}
                          >
                            Delete
                          </Menu.Item>
                        </Menu.Popup>
                      </Menu.Positioner>
                    </Menu.Portal>
                  </Menu.Root>
                </li>
              );
            })}
          </ul>
        )}
      </div>

      {renaming && (
        <RenameDialog conversation={renaming} workspaceId={workspaceId} onClose={() => setRenaming(null)} />
      )}
      {deleting && (
        <DeleteDialog
          conversation={deleting}
          workspaceId={workspaceId}
          onClose={() => setDeleting(null)}
          onDeleted={handleDeleted}
        />
      )}
    </aside>
  );
}

function MessageBubble({ message }: { message: Message }) {
  const mine = message.role === "user";
  return (
    <li className={cn("flex flex-col gap-1", mine ? "items-end" : "items-start")}>
      <span className="sr-only">{mine ? "You said:" : "Assistant said:"}</span>
      {/* Message text is shown as plain text. It is never rendered as HTML. */}
      <p
        className={cn(
          "max-w-[85%] rounded-2xl px-4 py-2 text-sm leading-6 break-words whitespace-pre-wrap",
          mine
            ? "rounded-br-md bg-zinc-900 text-white"
            : "rounded-bl-md border border-zinc-200 bg-white text-zinc-900",
        )}
      >
        {message.content}
      </p>
      <time dateTime={message.created_at} className="px-1 text-xs text-zinc-500">
        {timeFormat.format(new Date(message.created_at))}
      </time>
    </li>
  );
}

function Composer({ workspaceId, conversation }: { workspaceId: string; conversation: Conversation }) {
  const queryClient = useQueryClient();
  const inputId = useId();
  const [draft, setDraft] = useState("");
  const [draftError, setDraftError] = useState<string | null>(null);
  // One ID per message, kept across retries of the same text, so that a message whose
  // response was lost is not stored twice when it is sent again.
  const pending = useRef<{ content: string; id: string } | null>(null);

  const send = useMutation({
    mutationFn: (content: string) => {
      if (pending.current?.content !== content) {
        pending.current = { content, id: crypto.randomUUID() };
      }
      return sendMessage(conversation.id, content, pending.current.id);
    },
    onSuccess: async () => {
      pending.current = null;
      setDraft("");
      // Show what the server stored, and pick up the new title and ordering.
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: conversationKeys.messages(workspaceId, conversation.id) }),
        queryClient.invalidateQueries({ queryKey: conversationKeys.list(workspaceId), exact: true }),
        queryClient.invalidateQueries({
          queryKey: conversationKeys.detail(workspaceId, conversation.id),
          exact: true,
        }),
      ]);
    },
  });

  function submit() {
    if (send.isPending) return;
    const problem = validateMessage(draft);
    setDraftError(problem);
    if (problem) return;
    send.mutate(draft);
  }

  function handleKeyDown(event: KeyboardEvent<HTMLTextAreaElement>) {
    // Enter sends; Shift+Enter makes a new line. While an input method is composing
    // (Japanese, Chinese, Korean), Enter confirms the composition and must not send.
    if (event.key === "Enter" && !event.shiftKey && !event.nativeEvent.isComposing) {
      event.preventDefault();
      submit();
    }
  }

  const archived = conversation.status === "archived";

  return (
    <form
      onSubmit={(event) => {
        event.preventDefault();
        submit();
      }}
      className="border-t border-zinc-200 p-3"
    >
      {send.isError && (
        <div className="mb-2">
          <Alert>{errorMessage(send.error)} Your message is still in the box; you can send it again.</Alert>
        </div>
      )}
      {draftError && (
        <p id={`${inputId}-error`} role="alert" className="mb-2 text-sm text-red-700">
          {draftError}
        </p>
      )}
      <div className="flex items-end gap-2">
        <label htmlFor={inputId} className="sr-only">
          Message
        </label>
        <textarea
          id={inputId}
          value={draft}
          onChange={(event) => {
            setDraft(event.target.value);
            if (draftError) setDraftError(null);
          }}
          onKeyDown={handleKeyDown}
          rows={2}
          maxLength={MAX_MESSAGE_LENGTH}
          placeholder={archived ? "This conversation is archived." : "Write a message…"}
          disabled={send.isPending || archived}
          aria-invalid={draftError ? true : undefined}
          aria-describedby={`${inputId}-hint${draftError ? ` ${inputId}-error` : ""}`}
          className="max-h-48 min-h-[2.75rem] flex-1 resize-y rounded-lg border border-zinc-200 bg-white px-3 py-2 text-sm text-zinc-900 placeholder:text-zinc-400 focus-visible:outline-2 focus-visible:outline-offset-1 focus-visible:outline-zinc-900 disabled:cursor-not-allowed disabled:bg-zinc-50"
        />
        <Button type="submit" loading={send.isPending} disabled={archived}>
          {send.isPending ? "Sending…" : "Send"}
        </Button>
      </div>
      <p id={`${inputId}-hint`} className="mt-2 text-xs text-zinc-500">
        Enter to send, Shift+Enter for a new line. Messages are saved; AI answers are not available yet.
      </p>
    </form>
  );
}

function ChatArea({ workspaceId, conversationId }: { workspaceId: string; conversationId: string }) {
  const endRef = useRef<HTMLDivElement | null>(null);

  const conversation = useQuery({
    queryKey: conversationKeys.detail(workspaceId, conversationId),
    queryFn: ({ signal }) => getConversation(conversationId, signal),
  });

  // The URL names both a workspace and a conversation. If they do not belong together,
  // show nothing rather than a conversation under the wrong workspace.
  const belongs = conversation.data?.workspace_id === workspaceId;

  const messages = useInfiniteQuery({
    queryKey: conversationKeys.messages(workspaceId, conversationId),
    queryFn: ({ pageParam, signal }) => listMessages(conversationId, pageParam, signal),
    initialPageParam: undefined as number | undefined,
    // "Next" here means further back in time: the page before the oldest one loaded.
    getNextPageParam: (oldest) => (oldest.has_more ? oldest.items[0]?.sequence_number : undefined),
    enabled: belongs,
  });

  const ordered = messages.data ? [...messages.data.pages].reverse().flatMap((page) => page.items) : [];
  const newest = ordered.at(-1)?.id;

  useEffect(() => {
    endRef.current?.scrollIntoView?.({ block: "end" });
  }, [newest]);

  const backLink = (
    <Link href={chatPath(workspaceId)} className={cn(buttonClass("ghost", "sm"), "md:hidden")}>
      All conversations
    </Link>
  );

  if (conversation.isPending) {
    return (
      <div className="flex flex-1 flex-col p-4">
        <LoadingRegion label="Loading conversation">
          <div className="flex flex-col gap-3">
            <Skeleton className="h-7 w-56" />
            <Skeleton className="ml-auto h-10 w-2/3" />
            <Skeleton className="ml-auto h-10 w-1/2" />
          </div>
        </LoadingRegion>
      </div>
    );
  }

  if (conversation.isError || !belongs) {
    const missing =
      !conversation.isError ||
      (conversation.error instanceof ApiError && [404, 422].includes(conversation.error.status));
    return (
      <div className="flex flex-1 flex-col items-center justify-center gap-4 p-6">
        <div className="w-full max-w-md">
          <ErrorState
            title={missing ? "Conversation not found" : "Could not load this conversation"}
            description={
              missing
                ? "It may have been deleted, or it is not one of your conversations."
                : errorMessage(conversation.error)
            }
            onRetry={missing ? undefined : () => void conversation.refetch()}
          />
        </div>
        <Link href={chatPath(workspaceId)} className={buttonClass("secondary")}>
          Back to conversations
        </Link>
      </div>
    );
  }

  return (
    <section aria-labelledby="conversation-title" className="flex min-w-0 flex-1 flex-col">
      <header className="flex items-center justify-between gap-3 border-b border-zinc-200 px-4 py-3">
        <h2 id="conversation-title" className="min-w-0 truncate text-base font-semibold text-zinc-900">
          {conversation.data.title}
        </h2>
        {backLink}
      </header>

      <div className="min-h-0 flex-1 overflow-y-auto bg-zinc-50/60 p-4">
        {messages.isPending ? (
          <LoadingRegion label="Loading messages">
            <div className="flex flex-col gap-3">
              <Skeleton className="ml-auto h-10 w-2/3" />
              <Skeleton className="ml-auto h-10 w-1/2" />
            </div>
          </LoadingRegion>
        ) : messages.isError ? (
          <ErrorState
            title="Could not load messages"
            description={errorMessage(messages.error)}
            onRetry={() => void messages.refetch()}
          />
        ) : ordered.length === 0 ? (
          <EmptyState
            title="No messages yet"
            description="Write your first message below. It is saved to this conversation, which only you can see. AI answers are not available yet; they arrive in the next milestone."
          />
        ) : (
          <>
            {messages.hasNextPage && (
              <div className="mb-4 flex justify-center">
                <Button
                  variant="secondary"
                  size="sm"
                  loading={messages.isFetchingNextPage}
                  onClick={() => void messages.fetchNextPage()}
                >
                  {messages.isFetchingNextPage ? "Loading…" : "Load earlier messages"}
                </Button>
              </div>
            )}
            <ol aria-label="Messages" className="flex flex-col gap-3">
              {ordered.map((message) => (
                <MessageBubble key={message.id} message={message} />
              ))}
            </ol>
          </>
        )}
        <div ref={endRef} />
      </div>

      <Composer workspaceId={workspaceId} conversation={conversation.data} />
    </section>
  );
}

/** Conversations for one workspace: the list on the left, the open conversation on the right. */
export function ChatWorkspace() {
  const { workspaceId, conversationId } = useParams<{ workspaceId: string; conversationId?: string }>();

  const workspace = useQuery({
    queryKey: workspaceKeys.detail(workspaceId),
    queryFn: ({ signal }) => getWorkspace(workspaceId, signal),
  });

  if (workspace.isPending) {
    return (
      <LoadingRegion label="Loading workspace">
        <div className="flex flex-col gap-6">
          <Skeleton className="h-9 w-64" />
          <Skeleton className="h-96 rounded-xl" />
        </div>
      </LoadingRegion>
    );
  }

  if (workspace.isError) {
    const missing = workspace.error instanceof ApiError && [404, 422].includes(workspace.error.status);
    return (
      <div className="flex flex-col items-center gap-4">
        <div className="w-full">
          <ErrorState
            title={missing ? "Workspace not found" : "Could not load this workspace"}
            description={
              missing
                ? "It may have been deleted, or you may not be a member of it."
                : errorMessage(workspace.error)
            }
            onRetry={missing ? undefined : () => void workspace.refetch()}
          />
        </div>
        <Link href="/dashboard/workspaces" className={buttonClass("secondary")}>
          Back to workspaces
        </Link>
      </div>
    );
  }

  return (
    <>
      <div className="mb-4 flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          <h1 className="text-2xl font-semibold tracking-tight break-words text-zinc-900">Chat</h1>
          <p className="mt-1 text-sm text-zinc-600">
            {workspace.data.name} · Your conversations here are private to you.
          </p>
        </div>
        <Link href={`/dashboard/workspaces/${workspaceId}`} className={buttonClass("secondary", "sm")}>
          Back to workspace
        </Link>
      </div>

      {/* Keyed by workspace, so nothing from one workspace's chat survives a switch to another. */}
      <div
        key={workspaceId}
        className="flex h-[calc(100dvh-13rem)] min-h-[28rem] overflow-hidden rounded-xl border border-zinc-200 bg-white"
      >
        <ConversationSidebar
          workspaceId={workspaceId}
          activeId={conversationId}
          hiddenOnMobile={Boolean(conversationId)}
        />
        {conversationId ? (
          <ChatArea key={conversationId} workspaceId={workspaceId} conversationId={conversationId} />
        ) : (
          <div className="hidden flex-1 items-center justify-center p-6 md:flex">
            <div className="max-w-sm">
              <EmptyState
                title="Select a conversation"
                description="Choose a conversation from the list, or start a new one."
              />
            </div>
          </div>
        )}
      </div>
    </>
  );
}
