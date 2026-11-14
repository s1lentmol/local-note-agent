import { useRef, useState, type FormEvent } from "react";
import { api } from "./api";

const storageKey = "local-note-chat-conversation";

function storedConversation(): string | null {
  try {
    return sessionStorage.getItem(storageKey);
  } catch {
    return null;
  }
}

function storeConversation(id: string | null) {
  try {
    if (id) sessionStorage.setItem(storageKey, id);
    else sessionStorage.removeItem(storageKey);
  } catch {
    // The current page can still continue the conversation in memory.
  }
}

export function Chat() {
  const [conversationId, setConversationId] = useState(storedConversation);
  const [turns, setTurns] = useState<{ question: string; answer: string }[]>([]);
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");
  const [sending, setSending] = useState(false);
  const pending = useRef(false);

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const question = message.trim();
    if (!question || pending.current) return;
    pending.current = true;
    setSending(true);
    setError("");
    try {
      const reply = await api.chat(question, conversationId);
      storeConversation(reply.conversation_id);
      setConversationId(reply.conversation_id);
      setTurns((previous) => [...previous, { question, answer: reply.text }]);
      setMessage("");
    } catch (error) {
      setError(error instanceof Error ? error.message : "Не удалось отправить сообщение.");
    } finally {
      pending.current = false;
      setSending(false);
    }
  }

  function startOver() {
    storeConversation(null);
    setConversationId(null);
    setTurns([]);
    setError("");
  }

  return (
    <section className="chat">
      <div className="page-heading">
        <h1>Чат по заметкам</h1>
        {conversationId && (
          <button type="button" disabled={sending} onClick={startOver}>
            Новый диалог
          </button>
        )}
      </div>
      {conversationId && turns.length === 0 && (
        <p className="muted">Продолжение текущего диалога</p>
      )}
      {turns.length > 0 && (
        <ol className="chat-turns" aria-label="Диалог" aria-live="polite">
          {turns.map(({ question, answer }, index) => (
            <li key={index}>
              <p className="chat-question"><strong>Вы</strong>{question}</p>
              <p><strong>Ответ</strong>{answer}</p>
            </li>
          ))}
        </ol>
      )}
      <form onSubmit={submit} aria-busy={sending}>
        <label htmlFor="chat-message">Сообщение</label>
        <textarea
          id="chat-message"
          value={message}
          onChange={(event) => setMessage(event.target.value)}
          rows={3}
          required
          disabled={sending}
        />
        {error && <p className="error" role="alert">{error}</p>}
        <div className="actions">
          <button className="primary" disabled={sending || !message.trim()}>
            {sending ? "Отправляем…" : "Отправить"}
          </button>
          {sending && <span role="status">Ищем материалы и готовим ответ…</span>}
        </div>
      </form>
    </section>
  );
}
