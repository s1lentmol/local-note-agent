export function ErrorBox({
  error,
  retry,
}: {
  error: Error;
  retry?: () => void;
}) {
  return (
    <div role="alert" className="error">
      <p>{error.message}</p>
      {retry && <button onClick={retry}>Повторить запрос</button>}
    </div>
  );
}
