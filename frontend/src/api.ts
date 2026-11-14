// Mirrors the implemented HTTP contracts in backend/app/dto.py.
export type SourceType =
  "text" | "text_file" | "url" | "image" | "audio" | "pdf";
export type IngestionStatus = "queued" | "processing" | "ready" | "failed";
export type ProcessingStatus = IngestionStatus | "not_requested";
export interface Failure {
  code: string;
  message: string;
}
export interface NoteItem {
  id: string;
  title: string;
  source_type: SourceType;
  ingestion_status: IngestionStatus;
  processing_status: ProcessingStatus;
  summary: string | null;
  tags: string[];
  created_at: string;
  updated_at: string;
}
export interface Note extends Omit<NoteItem, "source_type"> {
  source: {
    type: SourceType;
    original_text: string | null;
    original_url: string | null;
    metadata: Record<string, unknown>;
  };
  extracted_text: string | null;
  ingestion_error: Failure | null;
  processing_error: Failure | null;
  category: string | null;
  entities: string[];
  attachments: {
    id: string;
    original_name: string;
    mime_type: string;
    size_bytes: number;
    download_url: string;
  }[];
}
export interface Page {
  items: NoteItem[];
  total: number;
  limit: number;
  offset: number;
}
export type ImportStatus = "queued" | "processing" | "done" | "failed";
export interface ImportAccepted {
  batch_id: string;
  collection_id: string;
  item_ids: string[];
}
export interface ImportDetail {
  id: string;
  collection_id: string | null;
  status: ImportStatus;
  total: number;
  succeeded: number;
  failed: number;
  items: {
    id: string;
    original_name: string;
    status: ImportStatus;
    note_id: string | null;
    error: Failure | null;
  }[];
}
export interface CollectionSummary {
  id: string;
  title: string;
  created_at: string;
  updated_at: string;
}
export interface CollectionDetail extends CollectionSummary {
  notes: Page;
}
export interface Query {
  limit: number;
  offset: number;
  source_type: SourceType | "";
  ingestion_status: IngestionStatus | "";
  processing_status: ProcessingStatus | "";
}
export class ApiError extends Error {
  constructor(
    public status: number,
    public code: string,
    message: string,
  ) {
    super(message);
  }
}
async function request<T>(path: string, options: RequestInit = {}): Promise<T> {
  let response: Response;
  try {
    response = await fetch(`/api/v1${path}`, options);
  } catch (error) {
    if (options.signal?.aborted) throw error;
    throw new ApiError(
      0,
      "network_error",
      "Нет связи с API. Проверьте подключение и повторите запрос.",
    );
  }
  if (!response.ok) {
    const body = await response.json().catch(() => null);
    throw new ApiError(
      response.status,
      body?.error?.code || "http_error",
      body?.error?.message || `API вернул ошибку ${response.status}.`,
    );
  }
  if (response.status === 204) return undefined as T;
  return response.json();
}
const json = (method: string, body: unknown): RequestInit => ({
  method,
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify(body),
});
export const api = {
  upload: (form: FormData) =>
    request<ImportAccepted>("/imports", { method: "POST", body: form }),
  importDetail: (id: string, signal: AbortSignal) =>
    request<ImportDetail>(`/imports/${encodeURIComponent(id)}`, { signal }),
  collections: (signal: AbortSignal) =>
    request<CollectionSummary[]>("/collections", { signal }),
  collection: (id: string, offset: number, signal: AbortSignal) =>
    request<CollectionDetail>(`/collections/${encodeURIComponent(id)}?offset=${offset}`, { signal }),
  createCollection: (title: string) =>
    request<CollectionSummary>("/collections", json("POST", { title })),
  addCollectionNotes: (id: string, noteIds: string[]) =>
    request<void>(`/collections/${encodeURIComponent(id)}/notes`, json("POST", { note_ids: noteIds })),
  removeCollectionNote: (id: string, noteId: string) =>
    request<void>(`/collections/${encodeURIComponent(id)}/notes/${encodeURIComponent(noteId)}`, {
      method: "DELETE",
    }),
  deleteCollection: (id: string) =>
    request<void>(`/collections/${encodeURIComponent(id)}`, { method: "DELETE" }),
  retryExtract: (id: string) =>
    request<Note>(`/notes/${encodeURIComponent(id)}/retry`, json("POST", { stage: "extract" })),
  chat: (message: string, conversationId: string | null) =>
    request<{ conversation_id: string; text: string }>(
      "/chat/messages",
      json("POST", { message, conversation_id: conversationId }),
    ),
  list(query: Query, signal: AbortSignal) {
    const params = new URLSearchParams();
    Object.entries(query).forEach(([key, value]) => {
      if (value !== "") params.set(key, String(value));
    });
    return request<Page>(`/notes?${params}`, { signal });
  },
  get: (id: string, signal: AbortSignal) =>
    request<Note>(`/notes/${encodeURIComponent(id)}`, { signal }),
  create: (text: string, title: string) =>
    request<Note>(
      "/notes",
      json("POST", {
        source_type: "text",
        text,
        ...(title.trim() ? { title: title.trim() } : {}),
      }),
    ),
  patch: (id: string, patch: { title?: string; extracted_text?: string }) =>
    request<Note>(`/notes/${encodeURIComponent(id)}`, json("PATCH", patch)),
  delete: (id: string) =>
    request<void>(`/notes/${encodeURIComponent(id)}`, { method: "DELETE" }),
};
export const active = (
  note: Pick<NoteItem, "ingestion_status" | "processing_status">,
) =>
  [note.ingestion_status, note.processing_status].some(
    (status) => status === "queued" || status === "processing",
  );
