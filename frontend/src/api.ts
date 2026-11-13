// Mirrors the implemented CRUD subset of backend/app/dto.py.
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
    response = await fetch(`/api/v1/notes${path}`, options);
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
  list(query: Query, signal: AbortSignal) {
    const params = new URLSearchParams();
    Object.entries(query).forEach(([key, value]) => {
      if (value !== "") params.set(key, String(value));
    });
    return request<Page>(`?${params}`, { signal });
  },
  get: (id: string, signal: AbortSignal) =>
    request<Note>(`/${encodeURIComponent(id)}`, { signal }),
  create: (text: string, title: string) =>
    request<Note>(
      "",
      json("POST", {
        source_type: "text",
        text,
        ...(title.trim() ? { title: title.trim() } : {}),
      }),
    ),
  patch: (id: string, patch: { title?: string; extracted_text?: string }) =>
    request<Note>(`/${encodeURIComponent(id)}`, json("PATCH", patch)),
  delete: (id: string) =>
    request<void>(`/${encodeURIComponent(id)}`, { method: "DELETE" }),
};
export const active = (
  note: Pick<NoteItem, "ingestion_status" | "processing_status">,
) =>
  [note.ingestion_status, note.processing_status].some(
    (status) => status === "queued" || status === "processing",
  );
