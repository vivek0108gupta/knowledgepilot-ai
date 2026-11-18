import { afterEach, describe, expect, test, vi } from "vitest";

import { ApiError, configureApiAuth } from "./api";
import {
  canManageDocument,
  fileTypeLabel,
  formatBytes,
  isInProgress,
  listDocuments,
  MAX_FILE_BYTES,
  uploadDocument,
  validateFile,
  type DocumentItem,
} from "./documents";

const ME = "11111111-1111-4111-8111-111111111111";
const OTHER = "22222222-2222-4222-8222-222222222222";
const WORKSPACE = "8a5c1f0e-0000-4000-8000-000000000000";

const DOCUMENT: DocumentItem = {
  id: "33333333-3333-4333-8333-333333333333",
  workspace_id: WORKSPACE,
  uploaded_by: OTHER,
  filename: "report.pdf",
  mime_type: "application/pdf",
  size_bytes: 2048,
  status: "uploaded",
  page_count: null,
  error: null,
  created_at: "2026-11-11T00:00:00Z",
  updated_at: "2026-11-11T00:00:00Z",
};

afterEach(() => {
  vi.unstubAllGlobals();
  configureApiAuth(null);
});

describe("file validation before upload", () => {
  test.each(["report.pdf", "notes.docx", "readme.txt", "REPORT.PDF", "my.file.name.Docx"])(
    "accepts %s",
    (name) => {
      expect(validateFile({ name, size: 1024 })).toBeNull();
    },
  );

  test.each([
    "malware.exe",
    "report.pdf.exe",
    "page.html",
    "old.doc",
    "macro.docm",
    "photo.png",
    "archive.zip",
    "noextension",
    ".pdf.sh",
  ])("rejects %s", (name) => {
    expect(validateFile({ name, size: 1024 })).toBe("Only PDF, DOCX and TXT files can be uploaded.");
  });

  test("rejects an empty file", () => {
    expect(validateFile({ name: "empty.pdf", size: 0 })).toBe("This file is empty.");
  });

  test("accepts a file of exactly 25 MB and rejects one byte more", () => {
    expect(validateFile({ name: "max.pdf", size: MAX_FILE_BYTES })).toBeNull();
    expect(validateFile({ name: "big.pdf", size: MAX_FILE_BYTES + 1 })).toBe(
      "This file is 25.0 MB. Files can be up to 25 MB.",
    );
  });

  test("the type is checked before the size, so the most useful message wins", () => {
    expect(validateFile({ name: "huge.exe", size: MAX_FILE_BYTES * 4 })).toMatch(/Only PDF/);
  });
});

describe("display helpers", () => {
  test("sizes are shown in the unit that fits", () => {
    expect(formatBytes(0)).toBe("0 B");
    expect(formatBytes(1023)).toBe("1023 B");
    expect(formatBytes(1024)).toBe("1.0 KB");
    expect(formatBytes(1536)).toBe("1.5 KB");
    expect(formatBytes(5 * 1024 * 1024)).toBe("5.0 MB");
  });

  test("file types have short names", () => {
    expect(fileTypeLabel("application/pdf")).toBe("PDF");
    expect(fileTypeLabel("text/plain")).toBe("Text");
    expect(fileTypeLabel("application/vnd.openxmlformats-officedocument.wordprocessingml.document")).toBe(
      "Word",
    );
    expect(fileTypeLabel("application/x-unknown")).toBe("File");
  });

  test("queued and processing documents count as in progress", () => {
    expect((["uploaded", "processing", "ready", "failed"] as const).map(isInProgress)).toEqual([
      true,
      true,
      false,
      false,
    ]);
  });
});

describe("who can delete or retry a document", () => {
  test("owners and admins can manage anyone's upload", () => {
    expect(canManageDocument("owner", DOCUMENT, ME)).toBe(true);
    expect(canManageDocument("admin", DOCUMENT, ME)).toBe(true);
  });

  test("a member can manage only their own upload", () => {
    expect(canManageDocument("member", DOCUMENT, ME)).toBe(false);
    expect(canManageDocument("member", { ...DOCUMENT, uploaded_by: ME }, ME)).toBe(true);
    expect(canManageDocument("member", DOCUMENT, undefined)).toBe(false);
  });
});

describe("listing", () => {
  function stubList() {
    const urls: string[] = [];
    vi.stubGlobal("fetch", async (input: RequestInfo | URL) => {
      urls.push(String(input));
      return Response.json({ items: [DOCUMENT], total: 1, limit: 10, offset: 0 });
    });
    return urls;
  }

  test("asks for one page, newest first, with no status filter by default", async () => {
    const urls = stubList();

    const page = await listDocuments(WORKSPACE, { status: "all", page: 0 });

    expect(urls).toEqual([
      `http://localhost:8000/api/v1/workspaces/${WORKSPACE}/documents?limit=10&offset=0`,
    ]);
    expect(page.items).toEqual([DOCUMENT]);
  });

  test("sends the status filter and the offset of the requested page", async () => {
    const urls = stubList();

    await listDocuments(WORKSPACE, { status: "failed", page: 2 });

    expect(urls[0]).toMatch(/\?limit=10&offset=20&status=failed$/);
  });
});

describe("uploading", () => {
  /** A scripted XMLHttpRequest: records what was sent and lets the test drive the outcome. */
  class FakeXhr {
    static last: FakeXhr;
    method = "";
    url = "";
    headers: Record<string, string> = {};
    body: FormData | null = null;
    status = 0;
    responseText = "";
    responseHeaders: Record<string, string> = {};
    upload: { onprogress: ((event: unknown) => void) | null } = { onprogress: null };
    onload: (() => void) | null = null;
    onerror: (() => void) | null = null;
    onabort: (() => void) | null = null;

    constructor() {
      FakeXhr.last = this;
    }
    open(method: string, url: string) {
      this.method = method;
      this.url = url;
    }
    setRequestHeader(name: string, value: string) {
      this.headers[name] = value;
    }
    getResponseHeader(name: string) {
      return this.responseHeaders[name] ?? null;
    }
    send(body: FormData) {
      this.body = body;
    }
    abort() {
      this.onabort?.();
    }
    respond(status: number, body: unknown) {
      this.status = status;
      this.responseText = typeof body === "string" ? body : JSON.stringify(body);
      this.onload?.();
    }
  }

  const file = new File(["%PDF-1.4 hello"], "report.pdf", { type: "application/pdf" });

  async function start(request?: Parameters<typeof uploadDocument>[2]) {
    vi.stubGlobal("XMLHttpRequest", FakeXhr);
    const promise = uploadDocument(WORKSPACE, file, request);
    await vi.waitFor(() => expect(FakeXhr.last?.body).not.toBeNull());
    return { promise, xhr: FakeXhr.last };
  }

  test("posts the file as multipart with the session token and returns the document", async () => {
    configureApiAuth({ getAccessToken: async () => "token-123", onUnauthorized: vi.fn() });
    const { promise, xhr } = await start();

    xhr.respond(201, DOCUMENT);

    await expect(promise).resolves.toEqual(DOCUMENT);
    expect(xhr.method).toBe("POST");
    expect(xhr.url).toBe(`http://localhost:8000/api/v1/workspaces/${WORKSPACE}/documents`);
    expect(xhr.headers.Authorization).toBe("Bearer token-123");
    // The browser must set Content-Type itself, or the multipart boundary is missing.
    expect(xhr.headers).not.toHaveProperty("Content-Type");
    expect((xhr.body!.get("file") as File).name).toBe("report.pdf");
  });

  test("reports progress as a fraction", async () => {
    const onProgress = vi.fn();
    const { promise, xhr } = await start({ onProgress });

    xhr.upload.onprogress?.({ lengthComputable: true, loaded: 25, total: 100 });
    xhr.upload.onprogress?.({ lengthComputable: true, loaded: 100, total: 100 });
    xhr.upload.onprogress?.({ lengthComputable: false, loaded: 5, total: 0 });
    xhr.respond(201, DOCUMENT);
    await promise;

    expect(onProgress.mock.calls).toEqual([[0.25], [1]]);
  });

  test("turns the server's rejection into an ApiError with its message", async () => {
    const { promise, xhr } = await start();

    xhr.respond(415, {
      error: { code: "content_mismatch", message: "The file's contents do not match its extension." },
    });

    await expect(promise).rejects.toMatchObject({
      status: 415,
      code: "content_mismatch",
      message: "The file's contents do not match its extension.",
    });
    await expect(promise).rejects.toBeInstanceOf(ApiError);
  });

  test("a 401 ends the session", async () => {
    const onUnauthorized = vi.fn();
    configureApiAuth({ getAccessToken: async () => "stale", onUnauthorized });
    const { promise, xhr } = await start();

    xhr.respond(401, { error: { code: "token_expired", message: "Expired." } });

    await expect(promise).rejects.toMatchObject({ status: 401 });
    expect(onUnauthorized).toHaveBeenCalledTimes(1);
  });

  test("a non-JSON error page is still an ApiError", async () => {
    const { promise, xhr } = await start();

    xhr.respond(413, "<html>Request Entity Too Large</html>");

    await expect(promise).rejects.toMatchObject({ status: 413, code: "unexpected_error" });
  });

  test("a network failure is reported as status 0", async () => {
    const { promise, xhr } = await start();

    xhr.onerror?.();

    await expect(promise).rejects.toMatchObject({ status: 0, code: "network_error" });
  });

  test("aborting the signal cancels the request", async () => {
    const controller = new AbortController();
    const { promise, xhr } = await start({ signal: controller.signal });
    const abort = vi.spyOn(xhr, "abort");

    controller.abort();

    await expect(promise).rejects.toMatchObject({ name: "AbortError" });
    expect(abort).toHaveBeenCalledTimes(1);
  });

  test("a success response of the wrong shape is rejected", async () => {
    const { promise, xhr } = await start();

    xhr.respond(201, { id: "not-a-document" });

    await expect(promise).rejects.toMatchObject({ code: "invalid_response" });
  });
});
