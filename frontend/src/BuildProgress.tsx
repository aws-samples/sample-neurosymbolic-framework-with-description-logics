interface Props {
  ontology: string;
  log: string;
}

export function BuildProgress({ ontology, log }: Props) {
  return (
    <div className="build">
      <h3>
        Building a query program for <b>{ontology}</b>
        <span className="dots" />
      </h3>
      <div className="build-note">
        No program exists yet for this ontology. Running the builder — this can
        take a while (it evolves and evaluates a program iteratively).
      </div>
      <pre>{log || "starting…"}</pre>
    </div>
  );
}
