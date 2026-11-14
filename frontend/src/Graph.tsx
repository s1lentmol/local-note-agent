import cytoscape from "cytoscape";
import { useEffect, useRef } from "react";
import { useResource } from "./useResource";
import "./Graph.css";

export interface GraphQuery {
  note_id?: string;
  collection_id?: string;
  limit: number;
}

export interface GraphResponse {
  nodes: { id: string; title: string }[];
  edges: { source: string; target: string; score: number }[];
}

interface GraphProps {
  noteId?: string;
  collectionId?: string;
  load: (query: GraphQuery, signal: AbortSignal) => Promise<GraphResponse>;
  onOpenNote: (id: string) => void;
}

const limit = 20;

export function Graph({ noteId, collectionId, load, onOpenNote }: GraphProps) {
  const canvas = useRef<HTMLDivElement>(null);
  const openNote = useRef(onOpenNote);
  openNote.current = onOpenNote;
  const { data, loading, error, refresh } = useResource(
    JSON.stringify([noteId, collectionId]),
    (signal) => load({ note_id: noteId, collection_id: collectionId, limit }, signal),
    () => false,
  );
  const graphData = error ? undefined : data;

  useEffect(() => {
    if (!canvas.current || !graphData?.nodes.length) return;
    const graph = cytoscape({
      container: canvas.current,
      elements: [
        ...graphData.nodes.map(({ id, title }) => ({
          data: { id, title },
          classes: id === noteId ? "anchor" : "",
        })),
        ...graphData.edges.map((edge) => ({ data: edge })),
      ],
      layout: { name: "circle", padding: 62, animate: false },
      maxZoom: 2,
      minZoom: 0.2,
      style: [
        {
          selector: "node",
          style: {
            label: "data(title)",
            "background-color": "#23606b",
            color: "#172b34",
            "font-size": 12,
            "text-wrap": "ellipsis",
            "text-max-width": "108px",
            "text-valign": "bottom",
            "text-margin-y": 5,
            "text-background-color": "#fff",
            "text-background-opacity": 0.9,
            "text-background-padding": "2px",
          },
        },
        {
          selector: "node.anchor",
          style: {
            "background-color": "#253c78",
            "border-color": "#a6bae8",
            "border-width": 4,
          },
        },
        {
          selector: "edge",
          style: { width: 2, "line-color": "#9db9bf", "curve-style": "straight" },
        },
      ],
    });
    graph.on("tap", "node", (event) => openNote.current(event.target.id()));
    return () => graph.destroy();
  }, [graphData, noteId]);

  return (
    <section className="note-graph" aria-busy={loading}>
      <h1>Граф заметок</h1>
      {loading && <p role="status">Загружаем граф…</p>}
      {error && (
        <div role="alert">
          <p className="error">Не удалось загрузить граф. {error.message}</p>
          <button type="button" onClick={refresh}>Повторить</button>
        </div>
      )}
      {!loading && graphData?.edges.length === 0 && (
        <p className="muted">Связей между заметками пока нет.</p>
      )}
      {graphData && graphData.nodes.length > 0 && (
        <>
          <div className="note-graph-canvas" ref={canvas} aria-hidden="true" />
          <ul className="note-graph-notes" aria-label="Заметки на графе">
            {graphData.nodes.map(({ id, title }) => (
              <li key={id}>
                <button type="button" onClick={() => onOpenNote(id)}>
                  {title}
                </button>
              </li>
            ))}
          </ul>
        </>
      )}
    </section>
  );
}
