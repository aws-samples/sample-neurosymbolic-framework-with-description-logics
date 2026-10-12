import { useCallback, useEffect, useState, type ReactNode } from "react";
import { api, type ProgramInfo } from "./api";
import { Markdown } from "./Markdown";

interface Props {
  ontology: string;
}

// Displays the current best query program for the selected ontology. Fetched
// whenever the ontology changes, and re-fetchable via the Refresh button so it
// reflects the most recently built program (e.g. after a rebuild).
export function ProgramView({ ontology }: Props) {
  const [program, setProgram] = useState<ProgramInfo | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  const load = useCallback(() => {
    if (!ontology) {
      setProgram(null);
      setError(null);
      return () => {};
    }
    let cancelled = false;
    setLoading(true);
    setError(null);
    api
      .program(ontology)
      .then((p) => {
        if (!cancelled) setProgram(p);
      })
      .catch((e: Error) => {
        if (!cancelled) {
          setProgram(null);
          setError(e.message);
        }
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [ontology]);

  useEffect(() => load(), [load]);

  const header = (
    <div className="program-header">
      <span className="program-path" title={program?.path}>
        {program?.path ?? (ontology ? `Query program · ${ontology}` : "Query program")}
      </span>
      <button
        className="ghost"
        onClick={() => load()}
        disabled={!ontology || loading}
        title="Reload the current query program"
      >
        {loading ? "Refreshing…" : "Refresh"}
      </button>
    </div>
  );

  let body: ReactNode;
  if (!ontology) {
    body = <div className="program-empty">Choose an ontology to view its query program.</div>;
  } else if (loading && !program) {
    body = <div className="program-empty">Loading query program for {ontology}…</div>;
  } else if (error) {
    body = (
      <div className="program-empty">
        No query program to show for <b>{ontology}</b>: {error}
      </div>
    );
  } else if (program) {
    const testSet = program.test_set ?? [];
    body = (
      <>
        {testSet.length > 0 && (
          <details className="test-set">
            <summary>Test set used to direct the evolution</summary>
            <ol className="test-set-list">
              {testSet.map((item, idx) => (
                <li key={idx}>
                  <details className="test-set-item">
                    <summary>{item.question}</summary>
                    <div className="test-set-answer">
                      <div className="test-set-answer-label">Expected answer</div>
                      {item.expected_answer.trim() ? (
                        <Markdown text={item.expected_answer} />
                      ) : (
                        <div className="program-empty">No expected answer recorded.</div>
                      )}
                    </div>
                  </details>
                </li>
              ))}
            </ol>
          </details>
        )}
        <pre className="program-source">{program.source}</pre>
      </>
    );
  } else {
    body = null;
  }

  return (
    <div className="program">
      {header}
      {body}
    </div>
  );
}
