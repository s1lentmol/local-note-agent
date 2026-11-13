import { useEffect, useRef, useState, type FormEvent } from "react";
import {
  active,
  api,
  type Failure,
  type Note,
  type NoteItem,
  type Query,
  type SourceType,
} from "./api";
import { useResource } from "./useResource";

const sources: Record<SourceType, string> = {
  text: "Текст",
  text_file: "Текстовый файл",
  url: "Ссылка",
  image: "Изображение",
  audio: "Аудио",
  pdf: "PDF",
};
const ingestion = {
  queued: "В очереди",
  processing: "Извлечение текста",
  ready: "Текст готов",
  failed: "Ошибка извлечения",
};
const processing = {
  not_requested: "Не запрошена",
  queued: "В очереди",
  processing: "Выполняется",
  ready: "Завершена",
  failed: "Ошибка обработки",
};
const date = (value: string) =>
  new Date(value).toLocaleString("ru-RU", {
    dateStyle: "medium",
    timeStyle: "short",
  });
function ErrorBox({ error, retry }: { error: Error; retry?: () => void }) {
  return (
    <div role="alert" className="error">
      <p>{error.message}</p>
      {retry && <button onClick={retry}>Повторить запрос</button>}
    </div>
  );
}
function StageError({
  title,
  error,
}: {
  title: string;
  error: Failure | null;
}) {
  return (
    error && (
      <div className="error" role="alert">
        <strong>{title}</strong>
        <p>{error.message}</p>
        <small>Код: {error.code}</small>
      </div>
    )
  );
}
function Status({
  note,
}: {
  note: Pick<NoteItem, "ingestion_status" | "processing_status">;
}) {
  return (
    <div className="statuses">
      <span className={`badge ${note.ingestion_status}`}>
        Извлечение: {ingestion[note.ingestion_status]}
      </span>
      <span className={`badge ${note.processing_status}`}>
        Обработка: {processing[note.processing_status]}
      </span>
    </div>
  );
}
function List() {
  const [query, setQuery] = useState<Query>({
    limit: 20,
    offset: 0,
    source_type: "",
    ingestion_status: "",
    processing_status: "",
  });
  const { data, error, loading, refresh } = useResource(
    JSON.stringify(query),
    (signal) => api.list(query, signal),
    (page) => page.items.some(active),
  );
  useEffect(() => {
    if (data && data.total > 0 && query.offset >= data.total)
      setQuery((q) => ({
        ...q,
        offset: Math.floor((data.total - 1) / q.limit) * q.limit,
      }));
    if (data && data.total === 0 && query.offset)
      setQuery((q) => ({ ...q, offset: 0 }));
  }, [data, query.offset]);
  const filtered = !!(
    query.source_type ||
    query.ingestion_status ||
    query.processing_status
  );
  return (
    <>
      <div className="page-heading">
        <div>
          <p className="eyebrow">ВАШИ МАТЕРИАЛЫ</p>
          <h1>Заметки</h1>
          <p className="muted">Сохраняйте мысли и возвращайтесь к ним.</p>
        </div>
        <a className="button primary" href="#/new">
          Создать заметку
        </a>
      </div>
      <section className="filters" aria-label="Фильтры заметок">
        <label>
          Источник
          <select
            value={query.source_type}
            onChange={(e) =>
              setQuery((q) => ({
                ...q,
                offset: 0,
                source_type: e.target.value as Query["source_type"],
              }))
            }
          >
            <option value="">Все источники</option>
            {Object.entries(sources).map(([key, label]) => (
              <option key={key} value={key}>
                {label}
              </option>
            ))}
          </select>
        </label>
        <label>
          Извлечение
          <select
            value={query.ingestion_status}
            onChange={(e) =>
              setQuery((q) => ({
                ...q,
                offset: 0,
                ingestion_status: e.target.value as Query["ingestion_status"],
              }))
            }
          >
            <option value="">Все состояния</option>
            {Object.entries(ingestion).map(([key, label]) => (
              <option key={key} value={key}>
                {label}
              </option>
            ))}
          </select>
        </label>
        <label>
          Обработка
          <select
            value={query.processing_status}
            onChange={(e) =>
              setQuery((q) => ({
                ...q,
                offset: 0,
                processing_status: e.target.value as Query["processing_status"],
              }))
            }
          >
            <option value="">Все состояния</option>
            {Object.entries(processing).map(([key, label]) => (
              <option key={key} value={key}>
                {label}
              </option>
            ))}
          </select>
        </label>
        <label>
          На странице
          <select
            value={query.limit}
            onChange={(e) =>
              setQuery((q) => ({
                ...q,
                offset: 0,
                limit: Number(e.target.value),
              }))
            }
          >
            {[10, 20, 50, 100].map((n) => (
              <option key={n}>{n}</option>
            ))}
          </select>
        </label>
      </section>
      <div className="toolbar">
        <p role="status">
          {loading ? "Загрузка заметок…" : `Найдено: ${data?.total ?? "—"}`}
        </p>
        <button onClick={refresh} disabled={loading}>
          Обновить
        </button>
      </div>
      {error && <ErrorBox error={error} retry={refresh} />}
      {data && (
        <>
          <ul className="notes" aria-label="Список заметок">
            {data.items.map((note) => (
              <li key={note.id}>
                <a className="note-link" href={`#/notes/${note.id}`}>
                  <div className="note-meta">
                    <span>{sources[note.source_type]}</span>
                    <time dateTime={note.created_at}>
                      {date(note.created_at)}
                    </time>
                  </div>
                  <h2>{note.title}</h2>
                  {note.summary && <p className="preview">{note.summary}</p>}
                  <Status note={note} />
                </a>
              </li>
            ))}
          </ul>
          {!data.items.length && (
            <div className="empty">
              <h2>
                {filtered
                  ? "По этим фильтрам ничего нет"
                  : "Здесь появятся ваши заметки"}
              </h2>
              <p>
                {filtered
                  ? "Измените фильтры или покажите все заметки."
                  : "Начните с текста — заголовок можно не указывать."}
              </p>
              {filtered ? (
                <button
                  onClick={() =>
                    setQuery((q) => ({
                      ...q,
                      offset: 0,
                      source_type: "",
                      ingestion_status: "",
                      processing_status: "",
                    }))
                  }
                >
                  Сбросить фильтры
                </button>
              ) : (
                <a href="#/new" className="button primary">
                  Создать заметку
                </a>
              )}
            </div>
          )}
          {data.total > 0 && (
            <nav className="pagination" aria-label="Страницы заметок">
              <button
                disabled={loading || query.offset === 0}
                onClick={() =>
                  setQuery((q) => ({
                    ...q,
                    offset: Math.max(0, q.offset - q.limit),
                  }))
                }
              >
                Назад
              </button>
              <span>
                Страница {Math.floor(query.offset / query.limit) + 1} из{" "}
                {Math.max(1, Math.ceil(data.total / query.limit))}
              </span>
              <button
                disabled={loading || query.offset + query.limit >= data.total}
                onClick={() =>
                  setQuery((q) => ({ ...q, offset: q.offset + q.limit }))
                }
              >
                Далее
              </button>
            </nav>
          )}
        </>
      )}
    </>
  );
}
// A lifetime token is replaced on every effect setup, including StrictMode replay.
// Ignore late mutation replies locally; the server may still commit the write.
function useOperationScope() {
  const lifetime = useRef({ active: true });
  useEffect(() => {
    const current = { active: true };
    lifetime.current = current;
    return () => {
      current.active = false;
    };
  }, []);
  return () => {
    const current = lifetime.current;
    const path = window.location.hash;
    // Also cover a changed hash before React has cleaned up the old screen.
    return () => current.active && window.location.hash === path;
  };
}

function NoteForm({
  note,
  onSave,
  onCancel,
}: {
  note?: Note;
  onSave: (note: Note) => void;
  onCancel: () => void;
}) {
  const beginOperation = useOperationScope();
  const [title, setTitle] = useState(note?.title || "");
  const [text, setText] = useState(note?.extracted_text || "");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<Error>();
  async function submit(event: FormEvent) {
    event.preventDefault();
    if (
      !text.trim() ||
      (note && !title.trim()) ||
      text.includes("\0") ||
      title.includes("\0")
    ) {
      setError(
        new Error(
          "Введите непустой текст и корректный заголовок, без символа NUL.",
        ),
      );
      return;
    }
    const isCurrent = beginOperation();
    setBusy(true);
    setError(undefined);
    try {
      const patch = {
        ...(title !== note?.title ? { title: title.trim() } : {}),
        ...(text !== note?.extracted_text ? { extracted_text: text } : {}),
      };
      if (note && !Object.keys(patch).length) {
        onCancel();
        return;
      }
      const saved = note
        ? await api.patch(note.id, patch)
        : await api.create(text, title);
      if (isCurrent()) onSave(saved);
    } catch (error) {
      if (isCurrent()) setError(error as Error);
    } finally {
      if (isCurrent()) setBusy(false);
    }
  }
  return (
    <form onSubmit={submit} className="editor">
      <fieldset disabled={busy}>
        <label>
          Заголовок{!note && <span className="muted"> · необязательно</span>}
          <input
            autoFocus
            value={title}
            onChange={(e) => setTitle(e.target.value)}
            maxLength={200}
            required={!!note}
            placeholder="Если оставить пустым — первая строка текста"
          />
        </label>
        <label>
          Текст
          <textarea
            value={text}
            onChange={(e) => setText(e.target.value)}
            maxLength={100000}
            required
            rows={14}
          />
        </label>
        <p className="muted counter">
          {text.length.toLocaleString("ru-RU")} / 100 000
        </p>
        <p className="muted">
          {note
            ? "Оригинал сохранится. Изменение сбросит прежние результаты обработки."
            : "Текст сохранится без изменений. Модели для сохранения не нужны."}
        </p>
        {error && <ErrorBox error={error} />}
        <div className="actions">
          <button className="primary" type="submit">
            {busy ? "Сохранение…" : "Сохранить"}
          </button>
          <button type="button" onClick={onCancel}>
            Отмена
          </button>
        </div>
      </fieldset>
    </form>
  );
}
function Detail({ id }: { id: string }) {
  const beginOperation = useOperationScope();
  const {
    data: note,
    error,
    loading,
    refresh,
  } = useResource(id, (signal) => api.get(id, signal), active);
  const [editing, setEditing] = useState(false);
  const [confirming, setConfirming] = useState(false);
  const [deleting, setDeleting] = useState(false);
  const [deleteError, setDeleteError] = useState<Error>();
  async function remove() {
    const isCurrent = beginOperation();
    setDeleting(true);
    setDeleteError(undefined);
    try {
      await api.delete(id);
      if (isCurrent()) window.location.hash = "/";
    } catch (error) {
      if (isCurrent()) setDeleteError(error as Error);
    } finally {
      if (isCurrent()) setDeleting(false);
    }
  }
  return (
    <>
      <a className="back" href="#/">
        ← Все заметки
      </a>
      <p role="status">
        {loading
          ? "Загрузка заметки…"
          : note && active(note)
            ? "Обработка выполняется · данные обновляются каждые 2 секунды"
            : ""}
      </p>
      {error && <ErrorBox error={error} retry={refresh} />}
      {note && (
        <article className="detail">
          <p className="eyebrow">{sources[note.source.type]}</p>
          <h1>{note.title}</h1>
          <p className="muted">
            Создана {date(note.created_at)} · Изменена {date(note.updated_at)}
          </p>
          <Status note={note} />
          <StageError
            title="Не удалось извлечь текст"
            error={note.ingestion_error}
          />
          <StageError
            title="Не удалось обработать заметку"
            error={note.processing_error}
          />
          {editing ? (
            <NoteForm
              note={note}
              onSave={() => {
                setEditing(false);
                refresh();
              }}
              onCancel={() => setEditing(false)}
            />
          ) : (
            <>
              <div className="actions">
                <button
                  disabled={note.ingestion_status !== "ready" || confirming}
                  onClick={() => setEditing(true)}
                >
                  Редактировать
                </button>
                <button onClick={refresh} disabled={loading}>
                  Обновить
                </button>
                <button
                  className="danger"
                  onClick={() => setConfirming(true)}
                  disabled={confirming}
                >
                  Удалить
                </button>
              </div>
              {note.ingestion_status !== "ready" && (
                <p className="muted">
                  Редактирование доступно после успешного извлечения текста.
                </p>
              )}
              {confirming && (
                <section
                  className="delete-confirm"
                  aria-label="Подтверждение удаления"
                >
                  <h2>Удалить заметку?</h2>
                  <p>
                    Она сразу исчезнет из списка. Восстановление недоступно.
                  </p>
                  {deleteError && <ErrorBox error={deleteError} />}
                  <div className="actions">
                    <button
                      className="danger"
                      disabled={deleting}
                      onClick={remove}
                    >
                      {deleting ? "Удаление…" : "Да, удалить"}
                    </button>
                    <button
                      disabled={deleting}
                      onClick={() => {
                        setConfirming(false);
                        setDeleteError(undefined);
                      }}
                    >
                      Отмена
                    </button>
                  </div>
                </section>
              )}
              <section>
                <h2>Текущий текст</h2>
                {note.extracted_text !== null ? (
                  <div className="full-text">{note.extracted_text}</div>
                ) : (
                  <p className="muted">
                    Текст пока не извлечён. Состояние указано выше.
                  </p>
                )}
              </section>
              <section className="original">
                <h2>Оригинал</h2>
                {note.source.original_text !== null ? (
                  <div className="full-text">{note.source.original_text}</div>
                ) : (
                  <>
                    <p className="muted">
                      Текстового оригинала у этого источника нет.
                    </p>
                    {note.source.original_url && (
                      <p className="full-text">
                        Исходная ссылка: {note.source.original_url}
                      </p>
                    )}
                    {note.attachments.map((file) => (
                      <p key={file.id}>
                        {file.original_name} · {file.mime_type} ·{" "}
                        {file.size_bytes.toLocaleString("ru-RU")} байт
                      </p>
                    ))}
                  </>
                )}
              </section>
            </>
          )}
        </article>
      )}
    </>
  );
}
function route() {
  return window.location.hash.slice(1) || "/";
}
export function App() {
  const [path, setPath] = useState(route);
  useEffect(() => {
    const change = () => {
      setPath(route());
      window.scrollTo?.(0, 0);
    };
    window.addEventListener("hashchange", change);
    return () => window.removeEventListener("hashchange", change);
  }, []);
  const match = /^\/notes\/([0-9a-f-]{36})$/i.exec(path);
  return (
    <>
      <header className="site-header">
        <a className="brand" href="#/">
          ◈ <span>Local Note</span>
        </a>
        <span className="muted">Место для ваших мыслей</span>
      </header>
      <main key={path}>
        {path === "/" ? (
          <List />
        ) : path === "/new" ? (
          <>
            <a className="back" href="#/">
              ← Все заметки
            </a>
            <h1>Новая заметка</h1>
            <NoteForm
              onSave={(note) => {
                window.location.hash = `/notes/${note.id}`;
              }}
              onCancel={() => {
                window.location.hash = "/";
              }}
            />
          </>
        ) : match ? (
          <Detail id={match[1]} />
        ) : (
          <>
            <h1>Страница не найдена</h1>
            <a href="#/">Все заметки</a>
          </>
        )}
      </main>
      <footer>Local Note · Заметки и состояния обработки</footer>
    </>
  );
}
