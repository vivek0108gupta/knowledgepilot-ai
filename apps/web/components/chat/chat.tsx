"use client";

import { Menu } from "@base-ui/react/menu";
import {
  useInfiniteQuery,
  useMutation,
  useQuery,
  useQueryClient,
  type InfiniteData,
} from "@tanstack/react-query";
import Link from "next/link";
import { useParams, useRouter } from "next/navigation";
import { useEffect, useId, useRef, useState, type FormEvent, type KeyboardEvent } from "react";

import { Button, buttonClass } from "@/components/ui/button";
import { Alert, EmptyState, ErrorState, LoadingRegion, Skeleton } from "@/components/ui/feedback";
import { TextField } from "@/components/ui/field";
import { Modal } from "@/components/ui/modal";
import { ApiError, errorMessage } from "@/lib/api";
import { cn } from "@/lib/cn";
import { requestCancel, streamQuestion, wasStopped } from "@/lib/chat-stream";
import {
  chatErrorMessage,
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
  questionWasKept,
  renameConversation,
  validateMessage,
  type Citation,
  type Conversation,
  type Message,
  type MessagePage,
} from "@/lib/conversations";
import { getDownload, startDownload } from "@/lib/documents";
import { sourceLabel } from "@/lib/search";
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

/** Splits an answer into text and its [1] / [1, 2] markers, so each marker can be a button. */
function splitMarkers(content: string): Array<string | number[]> {
  const parts: Array<string | number[]> = [];
  let last = 0;
  for (const match of content.matchAll(/\[(\d+(?:\s*,\s*\d+)*)\]/g)) {
    if (match.index > last) parts.push(content.slice(last, match.index));
    parts.push(match[1]!.split(",").map((value) => Number(value.trim())));
    last = match.index + match[0].length;
  }
  if (last < content.length) parts.push(content.slice(last));
  return parts;
}

function SourceCard({
  citation,
  cardId,
  open,
  onToggle,
}: {
  citation: Citation;
  cardId: string;
  open: boolean;
  onToggle: () => void;
}) {
  const excerptId = `${cardId}-excerpt`;
  const download = useMutation({
    mutationFn: () => getDownload(citation.document_id),
    onSuccess: (link) => startDownload(link.url),
  });
  const location = sourceLabel(citation);

  return (
    <li id={cardId} className="rounded-lg border border-zinc-200 bg-white p-3 text-sm">
      <div className="flex flex-wrap items-start justify-between gap-2">
        <div className="flex min-w-0 items-start gap-2">
          <span
            aria-hidden="true"
            className="grid size-5 shrink-0 place-items-center rounded bg-zinc-900 text-[11px] font-semibold text-white"
          >
            {citation.number}
          </span>
          <div className="min-w-0">
            <p className="font-medium break-words text-zinc-900">
              <span className="sr-only">Source {citation.number}: </span>
              {citation.document_name}
            </p>
            {location && <p className="text-xs text-zinc-500">{location}</p>}
          </div>
        </div>
        <div className="flex gap-1">
          <Button variant="ghost" size="sm" aria-expanded={open} aria-controls={excerptId} onClick={onToggle}>
            {open ? "Hide excerpt" : "Show excerpt"}
          </Button>
          <Button
            variant="ghost"
            size="sm"
            loading={download.isPending}
            aria-label={`Download ${citation.document_name}`}
            onClick={() => download.mutate()}
          >
            {download.isPending ? "Preparing…" : "Download"}
          </Button>
        </div>
      </div>
      {/* Document text is shown as plain text, never rendered as HTML. */}
      <blockquote
        id={excerptId}
        hidden={!open}
        className="mt-2 border-l-2 border-zinc-300 pl-3 text-zinc-700 whitespace-pre-line"
      >
        {citation.excerpt}
      </blockquote>
      {download.isError && (
        <div className="mt-2">
          <Alert>{errorMessage(download.error)}</Alert>
        </div>
      )}
    </li>
  );
}

function UserBubble({
  content,
  createdAt,
  sending,
}: {
  content: string;
  createdAt?: string;
  sending?: boolean;
}) {
  return (
    <li className="flex flex-col items-end gap-1">
      <span className="sr-only">You said:</span>
      {/* Message text is shown as plain text. It is never rendered as HTML. */}
      <p
        className={cn(
          "max-w-[85%] rounded-2xl rounded-br-md bg-zinc-900 px-4 py-2 text-sm leading-6 break-words whitespace-pre-wrap text-white",
          sending && "opacity-70",
        )}
      >
        {content}
      </p>
      {createdAt ? (
        <time dateTime={createdAt} className="px-1 text-xs text-zinc-500">
          {timeFormat.format(new Date(createdAt))}
        </time>
      ) : (
        <span className="px-1 text-xs text-zinc-500">Sending…</span>
      )}
    </li>
  );
}

function AssistantBubble({ message }: { message: Message }) {
  const baseId = useId();
  const [open, setOpen] = useState<number | null>(null);
  const grounded = message.metadata?.grounded !== false;
  const citations = message.citations ?? [];

  function reveal(number: number) {
    setOpen(number);
    document.getElementById(`${baseId}-source-${number}`)?.scrollIntoView?.({ block: "nearest" });
  }

  return (
    <li className="flex flex-col items-start gap-1">
      <span className="sr-only">Assistant said:</span>
      <div
        className={cn(
          "max-w-[85%] rounded-2xl rounded-bl-md border px-4 py-2 text-sm leading-6",
          grounded ? "border-zinc-200 bg-white text-zinc-900" : "border-amber-200 bg-amber-50 text-amber-900",
        )}
      >
        <p className="break-words whitespace-pre-wrap">
          {splitMarkers(message.content).map((part, index) =>
            typeof part === "string" ? (
              part
            ) : (
              <span key={index} className="whitespace-nowrap">
                [
                {part.map((number, position) => {
                  const known = citations.some((citation) => citation.number === number);
                  return (
                    <span key={number}>
                      {position > 0 && ", "}
                      {known ? (
                        <button
                          type="button"
                          onClick={() => reveal(number)}
                          aria-label={`Show source ${number}`}
                          className="rounded px-0.5 font-medium text-zinc-900 underline decoration-zinc-400 underline-offset-2 hover:bg-zinc-100 focus-visible:outline-2 focus-visible:outline-zinc-900"
                        >
                          {number}
                        </button>
                      ) : (
                        number
                      )}
                    </span>
                  );
                })}
                ]
              </span>
            ),
          )}
        </p>
        {!grounded && (
          <p className="mt-1 text-xs text-amber-800">
            No supporting passage was found in this workspace&apos;s documents.
          </p>
        )}
      </div>

      {citations.length > 0 && (
        <section aria-label="Sources" className="w-full max-w-[85%]">
          <h3 className="mt-1 mb-1.5 text-xs font-medium tracking-wide text-zinc-500 uppercase">Sources</h3>
          <ol className="flex flex-col gap-2">
            {citations.map((citation) => (
              <SourceCard
                key={citation.number}
                citation={citation}
                cardId={`${baseId}-source-${citation.number}`}
                open={open === citation.number}
                onToggle={() => setOpen(open === citation.number ? null : citation.number)}
              />
            ))}
          </ol>
        </section>
      )}

      <time dateTime={message.created_at} className="px-1 text-xs text-zinc-500">
        {timeFormat.format(new Date(message.created_at))}
      </time>
    </li>
  );
}

/**
 * The answer while it is being written. Its text is provisional: it has no source markers, and
 * is replaced by the checked answer, with its sources, when writing finishes.
 */
function StreamingBubble({ text }: { text: string }) {
  return (
    <li className="flex flex-col items-start gap-1" aria-busy="true">
      <span className="sr-only">Assistant is writing:</span>
      <div className="max-w-[85%] rounded-2xl rounded-bl-md border border-zinc-200 bg-white px-4 py-2 text-sm leading-6 text-zinc-900">
        {text ? (
          <p className="break-words whitespace-pre-wrap">
            {text}
            <span
              aria-hidden="true"
              className="ml-0.5 inline-block h-4 w-1.5 animate-pulse bg-zinc-400 align-text-bottom"
            />
          </p>
        ) : (
          <span aria-hidden="true" data-testid="typing-indicator" className="flex h-6 items-center gap-1">
            {[0, 150, 300].map((delay) => (
              <span
                key={delay}
                className="size-1.5 animate-bounce rounded-full bg-zinc-400"
                style={{ animationDelay: `${delay}ms` }}
              />
            ))}
          </span>
        )}
      </div>
      <span className="px-1 text-xs text-zinc-500">Sources are checked when the answer is finished.</span>
    </li>
  );
}

function MessageBubble({ message }: { message: Message }) {
  return message.role === "user" ? (
    <UserBubble content={message.content} createdAt={message.created_at} />
  ) : (
    <AssistantBubble message={message} />
  );
}

interface Failure {
  message: string;
  content: string;
  id: string;
  kept: boolean;
}

interface ComposerProps {
  conversation: Conversation;
  draft: string;
  onDraftChange: (draft: string) => void;
  sending: boolean;
  failure: Failure | null;
  onSend: () => void;
  onRetry: () => void;
  onStop: () => void;
}

function Composer({
  conversation,
  draft,
  onDraftChange,
  sending,
  failure,
  onSend,
  onRetry,
  onStop,
}: ComposerProps) {
  const inputId = useId();
  const [draftError, setDraftError] = useState<string | null>(null);

  function submit() {
    if (sending) return;
    const problem = validateMessage(draft);
    setDraftError(problem);
    if (!problem) onSend();
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
      {failure && (
        <div className="mb-2">
          <Alert>
            <span>{failure.message}</span>
            {failure.kept && (
              <Button variant="secondary" size="sm" className="ml-2" onClick={onRetry} disabled={sending}>
                Try again
              </Button>
            )}
          </Alert>
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
            onDraftChange(event.target.value);
            if (draftError) setDraftError(null);
          }}
          onKeyDown={handleKeyDown}
          rows={2}
          maxLength={MAX_MESSAGE_LENGTH}
          placeholder={archived ? "This conversation is archived." : "Ask a question about your documents…"}
          disabled={sending || archived}
          aria-invalid={draftError ? true : undefined}
          aria-describedby={`${inputId}-hint${draftError ? ` ${inputId}-error` : ""}`}
          className="max-h-48 min-h-[2.75rem] flex-1 resize-y rounded-lg border border-zinc-200 bg-white px-3 py-2 text-sm text-zinc-900 placeholder:text-zinc-400 focus-visible:outline-2 focus-visible:outline-offset-1 focus-visible:outline-zinc-900 disabled:cursor-not-allowed disabled:bg-zinc-50"
        />
        <div className="flex shrink-0 flex-col gap-1 sm:flex-row">
          {sending && (
            <Button variant="secondary" onClick={onStop}>
              Stop
            </Button>
          )}
          <Button type="submit" loading={sending} disabled={archived}>
            {sending ? "Answering…" : "Send"}
          </Button>
        </div>
      </div>
      <p id={`${inputId}-hint`} className="mt-2 text-xs text-zinc-500">
        Enter to send, Shift+Enter for a new line. Answers come only from this workspace&apos;s documents.
      </p>
    </form>
  );
}

function ChatArea({ workspaceId, conversationId }: { workspaceId: string; conversationId: string }) {
  const queryClient = useQueryClient();
  const endRef = useRef<HTMLDivElement | null>(null);
  const [draft, setDraft] = useState("");
  const [pending, setPending] = useState<{ content: string; id: string } | null>(null);
  const [failure, setFailure] = useState<Failure | null>(null);
  // The answer as it is being written, and whether the server has started on it.
  const [streamed, setStreamed] = useState("");
  const [started, setStarted] = useState<Message | null>(null);
  const controller = useRef<AbortController | null>(null);

  const conversation = useQuery({
    queryKey: conversationKeys.detail(workspaceId, conversationId),
    queryFn: ({ signal }) => getConversation(conversationId, signal),
  });

  // The URL names both a workspace and a conversation. If they do not belong together,
  // show nothing rather than a conversation under the wrong workspace.
  const belongs = conversation.data?.workspace_id === workspaceId;
  const messagesKey = conversationKeys.messages(workspaceId, conversationId);

  const messages = useInfiniteQuery({
    queryKey: messagesKey,
    queryFn: ({ pageParam, signal }) => listMessages(conversationId, pageParam, signal),
    initialPageParam: undefined as number | undefined,
    // "Next" here means further back in time: the page before the oldest one loaded.
    getNextPageParam: (oldest) => (oldest.has_more ? oldest.items[0]?.sequence_number : undefined),
    enabled: belongs,
  });

  // The list and the heading may have changed (a new title, a new order). The messages
  // themselves come back in the answer, so they are refetched only after a failure.
  const refreshConversation = () =>
    Promise.all([
      queryClient.invalidateQueries({ queryKey: conversationKeys.list(workspaceId), exact: true }),
      queryClient.invalidateQueries({
        queryKey: conversationKeys.detail(workspaceId, conversationId),
        exact: true,
      }),
    ]);

  const ask = useMutation({
    mutationFn: (question: { content: string; id: string }) => {
      const abort = new AbortController();
      controller.current = abort;
      return streamQuestion(
        conversationId,
        question.content,
        question.id,
        {
          onStarted: (event) => setStarted(event.user_message),
          onDelta: (text) => setStreamed((current) => current + text),
        },
        abort.signal,
      );
    },
    onMutate: (question) => {
      setFailure(null);
      setPending(question);
      setStreamed("");
      setStarted(null);
    },
    onSettled: () => {
      controller.current = null;
      setStreamed("");
      setStarted(null);
    },
    onSuccess: (result) => {
      // Show the stored question and answer at once, then refresh in the background.
      queryClient.setQueryData<InfiniteData<MessagePage, number | undefined>>(messagesKey, (data) => {
        if (!data || data.pages.length === 0) return data;
        const [newest, ...older] = data.pages;
        const known = new Set(data.pages.flatMap((page) => page.items.map((item) => item.id)));
        const added = [result.user_message, result.assistant_message].filter((item) => !known.has(item.id));
        return {
          ...data,
          pages: [
            { ...newest!, items: [...newest!.items, ...added], total: newest!.total + added.length },
            ...older,
          ],
        };
      });
      setPending(null);
      void refreshConversation();
    },
    onError: (error, question) => {
      if (wasStopped(error)) {
        // Stopping abandons the answer. The question is kept (or was never stored), and
        // trying again with the same ID answers it without storing it twice.
        setFailure({ message: "You stopped this answer. Your question was kept.", ...question, kept: true });
        setPending(null);
        void queryClient.invalidateQueries({ queryKey: messagesKey });
        void refreshConversation();
        return;
      }
      const kept = questionWasKept(error);
      setFailure({ message: chatErrorMessage(error), ...question, kept });
      setPending(null);
      // A question that was not stored goes back into the box so nothing is lost.
      if (!kept) setDraft((current) => current || question.content);
      // Show the stored question, if it was stored.
      void queryClient.invalidateQueries({ queryKey: messagesKey });
      void refreshConversation();
    },
  });

  function send() {
    const content = draft;
    // Resending the text of a failed question reuses its ID, so it is not stored twice.
    const id = failure && failure.content === content ? failure.id : crypto.randomUUID();
    setDraft("");
    ask.mutate({ content, id });
  }

  function retry() {
    if (failure) ask.mutate({ content: failure.content, id: failure.id });
  }

  function stop() {
    const running = controller.current;
    if (!running) return;
    // Label the stop as the person's choice first, then close the stream.
    void requestCancel(conversationId).finally(() => running.abort());
  }

  const ordered = messages.data ? [...messages.data.pages].reverse().flatMap((page) => page.items) : [];
  const newest = ordered.at(-1)?.id;

  useEffect(() => {
    endRef.current?.scrollIntoView?.({ block: "end" });
  }, [newest, pending, streamed]);

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
        ) : ordered.length === 0 && !pending ? (
          <EmptyState
            title="No messages yet"
            description="Ask a question about this workspace's documents. Answers come only from those documents and show the passages they rely on. Only you can see this conversation."
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
              {pending && (
                <UserBubble content={pending.content} createdAt={started?.created_at} sending={!started} />
              )}
              {pending && <StreamingBubble text={streamed} />}
            </ol>
            {pending && (
              <div role="status" className="mt-3 flex items-center gap-2 text-sm text-zinc-500">
                <span aria-hidden="true" className="size-2 animate-pulse rounded-full bg-zinc-400" />
                {streamed ? "Writing an answer…" : "Searching your documents and writing an answer…"}
              </div>
            )}
          </>
        )}
        <div ref={endRef} />
      </div>

      <Composer
        conversation={conversation.data}
        draft={draft}
        onDraftChange={setDraft}
        sending={ask.isPending}
        failure={failure}
        onSend={send}
        onRetry={retry}
        onStop={stop}
      />
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
