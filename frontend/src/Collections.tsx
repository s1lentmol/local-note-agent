import { useEffect, useState, type FormEvent } from "react";
import { active, api } from "./api";
import { ErrorBox } from "./ErrorBox";
import { useOperationScope } from "./useOperationScope";
import { useResource } from "./useResource";

const limit = 20;

export function Collections() {
  const beginOperation = useOperationScope();
  const { data, error, loading, refresh } = useResource(
    "collections",
    api.collections,
    () => false,
  );
  const [title, setTitle] = useState("");
  const [busy, setBusy] = useState(false);
  const [createError, setCreateError] = useState<Error>();
  async function create(event: FormEvent) {
    event.preventDefault();
    const isCurrent = beginOperation();
    setBusy(true);
    setCreateError(undefined);
    try {
      const collection = await api.createCollection(title.trim());
      if (isCurrent()) window.location.hash = `/collections/${collection.id}`;
    } catch (error) {
      if (isCurrent()) setCreateError(error as Error);
    } finally {
      if (isCurrent()) setBusy(false);
    }
  }
  return (
    <>
      <h1>Подборки</h1>
      <form className="editor" onSubmit={create}>
        <fieldset disabled={busy}>
          <label>
            Название новой подборки
            <input
              value={title}
              onChange={(event) => setTitle(event.target.value)}
              maxLength={200}
              required
            />
          </label>
          <div className="actions">
            <button className="primary">Создать подборку</button>
          </div>
        </fieldset>
      </form>
      {createError && <ErrorBox error={createError} />}
      {error && <ErrorBox error={error} retry={refresh} />}
      {loading && <p role="status">Загрузка подборок…</p>}
      {data?.length === 0 && <p className="muted">Подборок пока нет.</p>}
      <ul className="notes">
        {data?.map((collection) => (
          <li key={collection.id}>
            <a className="note-link" href={`#/collections/${collection.id}`}>
              {collection.title}
            </a>
          </li>
        ))}
      </ul>
    </>
  );
}

function AddNote({
  onAdd,
  busy,
}: {
  onAdd: (id: string) => Promise<void>;
  busy: boolean;
}) {
  const [noteId, setNoteId] = useState("");
  const [offset, setOffset] = useState(0);
  const candidates = useResource(
    String(offset),
    (signal) =>
      api.list(
        {
          limit,
          offset,
          source_type: "",
          ingestion_status: "",
          processing_status: "",
        },
        signal,
      ),
    () => false,
  );
  const selected = candidates.data?.items.some((note) => note.id === noteId)
    ? noteId
    : "";
  return (
    <form
      className="editor"
      onSubmit={(event) => {
        event.preventDefault();
        if (selected) void onAdd(selected);
      }}
    >
      <fieldset disabled={busy}>
        <label>
          Заметка для добавления
          <select
            value={selected}
            onChange={(event) => setNoteId(event.target.value)}
            required
          >
            <option value="">Выберите заметку</option>
            {candidates.data?.items.map((note) => (
              <option key={note.id} value={note.id}>
                {note.title}
              </option>
            ))}
          </select>
        </label>
        <div className="actions">
          <button className="primary" disabled={!selected}>
            Добавить
          </button>
          <button
            type="button"
            disabled={offset === 0 || candidates.loading}
            onClick={() => setOffset(offset - limit)}
          >
            Предыдущие варианты
          </button>
          <button
            type="button"
            disabled={
              !candidates.data ||
              offset + limit >= candidates.data.total ||
              candidates.loading
            }
            onClick={() => setOffset(offset + limit)}
          >
            Следующие варианты
          </button>
        </div>
      </fieldset>
      {candidates.error && (
        <ErrorBox error={candidates.error} retry={candidates.refresh} />
      )}
    </form>
  );
}

export function Collection({ id }: { id: string }) {
  const beginOperation = useOperationScope();
  const [offset, setOffset] = useState(0);
  const [busy, setBusy] = useState(false);
  const [confirming, setConfirming] = useState(false);
  const [adding, setAdding] = useState(false);
  const [operationError, setOperationError] = useState<Error>();
  const collection = useResource(
    `${id}:${offset}`,
    (signal) => api.collection(id, offset, signal),
    (value) => value.notes.items.some(active),
  );
  const notes = collection.data?.notes;
  useEffect(() => {
    if (notes && offset && offset >= notes.total) {
      setOffset(Math.max(0, Math.floor((notes.total - 1) / limit) * limit));
    }
  }, [notes, offset]);

  async function mutate(operation: () => Promise<void>, after: () => void) {
    const isCurrent = beginOperation();
    setBusy(true);
    setOperationError(undefined);
    try {
      await operation();
      if (isCurrent()) after();
    } catch (error) {
      if (isCurrent()) setOperationError(error as Error);
    } finally {
      if (isCurrent()) setBusy(false);
    }
  }

  return (
    <>
      <a className="back" href="#/collections">
        ← Все подборки
      </a>
      {collection.error && (
        <ErrorBox error={collection.error} retry={collection.refresh} />
      )}
      {collection.loading && !notes && <p role="status">Загрузка подборки…</p>}
      {collection.data && notes && (
        <>
          <h1>{collection.data.title}</h1>
          <div className="actions">
            <button
              disabled={busy}
              onClick={() => setAdding(!adding)}
              aria-expanded={adding}
            >
              {adding ? "Закрыть выбор заметки" : "Добавить заметку"}
            </button>
            <button
              className="danger"
              disabled={busy || confirming}
              onClick={() => setConfirming(true)}
            >
              Удалить подборку
            </button>
          </div>
          {operationError && <ErrorBox error={operationError} />}
          {confirming && (
            <section className="delete-confirm">
              <p>Удалить подборку? Её заметки останутся в общем списке.</p>
              <div className="actions">
                <button
                  className="danger"
                  disabled={busy}
                  onClick={() =>
                    void mutate(
                      () => api.deleteCollection(id),
                      () => {
                        window.location.hash = "/collections";
                      },
                    )
                  }
                >
                  Удалить
                </button>
                <button disabled={busy} onClick={() => setConfirming(false)}>
                  Отмена
                </button>
              </div>
            </section>
          )}
          {adding && (
            <AddNote
              busy={busy}
              onAdd={(noteId) =>
                mutate(
                  () => api.addCollectionNotes(id, [noteId]),
                  () => {
                    setAdding(false);
                    collection.refresh();
                  },
                )
              }
            />
          )}
          <p role="status">Заметок в подборке: {notes.total}</p>
          <ul className="notes">
            {notes.items.map((note) => (
              <li key={note.id} className="material-row">
                <a href={`#/notes/${note.id}`}>{note.title}</a>
                <button
                  disabled={busy}
                  onClick={() =>
                    void mutate(
                      () => api.removeCollectionNote(id, note.id),
                      collection.refresh,
                    )
                  }
                >
                  Исключить
                </button>
              </li>
            ))}
          </ul>
          {notes.total > limit && (
            <nav className="pagination" aria-label="Страницы подборки">
              <button
                disabled={offset === 0 || collection.loading}
                onClick={() => setOffset(offset - limit)}
              >
                Назад
              </button>
              <span>
                {Math.floor(offset / limit) + 1} /{" "}
                {Math.ceil(notes.total / limit)}
              </span>
              <button
                disabled={offset + limit >= notes.total || collection.loading}
                onClick={() => setOffset(offset + limit)}
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
