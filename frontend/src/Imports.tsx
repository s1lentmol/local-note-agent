import { useState, type FormEvent } from "react";
import { api, type ImportStatus } from "./api";
import { ErrorBox } from "./ErrorBox";
import { useOperationScope } from "./useOperationScope";
import { useResource } from "./useResource";

const maxFiles = 20;
const statuses: Record<ImportStatus, string> = {
  queued: "В очереди",
  processing: "Извлечение текста",
  done: "Текст извлечён",
  failed: "Ошибка",
};

export function Upload() {
  const beginOperation = useOperationScope();
  const collections = useResource("collections", api.collections, () => false);
  const [destination, setDestination] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<Error>();
  const [batchId, setBatchId] = useState("");

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    const files = form.getAll("files") as File[];
    if (files.length > maxFiles) {
      setError(new Error(`Можно загрузить до ${maxFiles} файлов.`));
      return;
    }
    if (destination && destination !== "new")
      form.set("collection_id", destination);
    const isCurrent = beginOperation();
    setBusy(true);
    setError(undefined);
    try {
      const result = await api.upload(form);
      if (isCurrent()) window.location.hash = `/imports/${result.batch_id}`;
    } catch (error) {
      if (isCurrent()) setError(error as Error);
    } finally {
      if (isCurrent()) setBusy(false);
    }
  }

  return (
    <>
      <h1>Загрузка файлов</h1>
      <form className="editor" onSubmit={submit}>
        <fieldset disabled={busy}>
          <label>
            Файлы
            <input
              type="file"
              name="files"
              multiple
              required
              accept=".txt,.md,.markdown,.ogg,.oga,.opus,.wav,.mp3"
            />
          </label>
          <p className="muted">
            До 20 файлов. TXT и Markdown в UTF-8 — до 1 МиБ на файл и 10 МиБ всего.
            OGG/Opus, WAV и MP3 — по умолчанию до 50 МиБ аудио суммарно и 30 минут на запись.
          </p>
          <label>
            Подборка
            <select
              value={destination}
              onChange={(event) => setDestination(event.target.value)}
            >
              <option value="">Создать подборку по дате импорта</option>
              <option value="new">Создать подборку с названием</option>
              {collections.data?.map((collection) => (
                <option key={collection.id} value={collection.id}>
                  {collection.title}
                </option>
              ))}
            </select>
          </label>
          {collections.error && (
            <ErrorBox error={collections.error} retry={collections.refresh} />
          )}
          {destination === "new" && (
            <label>
              Название подборки
              <input name="collection_title" maxLength={200} required />
            </label>
          )}
          {error && <ErrorBox error={error} />}
          <div className="actions">
            <button className="primary">
              {busy ? "Загрузка…" : "Загрузить файлы"}
            </button>
          </div>
        </fieldset>
      </form>
      <form
        className="editor"
        onSubmit={(event) => {
          event.preventDefault();
          window.location.hash = `/imports/${batchId.trim()}`;
        }}
      >
        <label>
          ID импорта из Telegram или веб-интерфейса
          <input
            value={batchId}
            onChange={(event) => setBatchId(event.target.value)}
            required
            pattern="[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
          />
        </label>
        <div className="actions">
          <button>Открыть результаты</button>
        </div>
      </form>
    </>
  );
}

export function ImportProgress({ id }: { id: string }) {
  const { data, error, loading, refresh } = useResource(
    id,
    (signal) => api.importDetail(id, signal),
    (batch) => batch.status === "queued" || batch.status === "processing",
  );
  return (
    <>
      <a className="back" href="#/imports">
        ← Загрузка файлов
      </a>
      <h1>Результаты импорта</h1>
      {error && <ErrorBox error={error} retry={refresh} />}
      {loading && !data && <p role="status">Загрузка результатов…</p>}
      {data && (
        <>
          <p role="status">
            Готово {data.succeeded} из {data.total} · Ошибок {data.failed} ·
            Осталось {data.total - data.succeeded - data.failed}
          </p>
          <progress
            aria-label="Завершённые файлы"
            value={data.succeeded + data.failed}
            max={data.total}
          />
          <div className="actions">
            {data.collection_id && (
              <a
                className="button"
                href={`#/collections/${data.collection_id}`}
              >
                Открыть подборку
              </a>
            )}
            <button onClick={refresh} disabled={loading}>
              Обновить
            </button>
          </div>
          <ul className="notes" aria-label="Файлы">
            {data.items.map((item) => (
              <li key={item.id} className="material-row">
                <div>
                  <strong>{item.original_name}</strong>
                  {item.error && (
                    <p className="error">
                      {item.error.message} · {item.error.code}
                    </p>
                  )}
                  {item.note_id && (
                    <p>
                      <a href={`#/notes/${item.note_id}`}>Открыть заметку</a>
                    </p>
                  )}
                </div>
                <span
                  className={`badge ${item.status === "done" ? "ready" : item.status}`}
                >
                  {statuses[item.status]}
                </span>
              </li>
            ))}
          </ul>
        </>
      )}
    </>
  );
}
