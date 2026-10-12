import { useEffect, useLayoutEffect, useRef } from "react";

interface Props {
  ontology: string;
  log: string;
  failed?: boolean;
  rebuilding?: boolean;
}

// Treat the view as "at the bottom" when within this many pixels of the end,
// so a nearly-bottomed user still gets pinned to new output.
const BOTTOM_THRESHOLD_PX = 40;

export function BuildProgress({ ontology, log, failed, rebuilding }: Props) {
  const preRef = useRef<HTMLPreElement>(null);
  // Whether the user is currently pinned to the bottom. Starts true so a fresh
  // build follows the latest lines; flips to false if the user scrolls up.
  const stickToBottom = useRef(true);

  const isAtBottom = (el: HTMLElement) =>
    el.scrollHeight - el.scrollTop - el.clientHeight <= BOTTOM_THRESHOLD_PX;

  const onScroll = () => {
    const el = preRef.current;
    if (el) stickToBottom.current = isAtBottom(el);
  };

  // After each log update, scroll to the newest lines — but only if the user
  // hasn't scrolled up to read earlier output. useLayoutEffect runs before
  // paint so the jump isn't visible as a flicker.
  useLayoutEffect(() => {
    const el = preRef.current;
    if (el && stickToBottom.current) {
      el.scrollTop = el.scrollHeight;
    }
  }, [log]);

  // Re-pin to the bottom when a new build starts (ontology changes).
  useEffect(() => {
    stickToBottom.current = true;
    const el = preRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [ontology]);

  return (
    <div className={`build${failed ? " failed" : ""}`}>
      <h3>
        {failed ? (
          <>
            Build failed for <b>{ontology}</b>
          </>
        ) : (
          <>
            {rebuilding ? "Rebuilding the query program for " : "Building a query program for "}
            <b>{ontology}</b>
            <span className="dots" />
          </>
        )}
      </h3>
      <div className="build-note">
        {failed
          ? "The builder exited with an error. The full output is below — a common cause is a missing OWL file for this ontology."
          : rebuilding
            ? "Rebuilding the query program from scratch (you forced a rebuild). The existing program stays in use until the new one succeeds, so a failed rebuild won't take it away. This can take a while — it evolves and evaluates a program iteratively, and errors and stacktraces below are normal: the builder tries new approaches, runs them, and self-corrects from the failures."
            : "No program exists yet for this ontology. Running the builder — this can take a while (it evolves and evaluates a program iteratively). It's normal to see errors and stacktraces below: the builder tries new approaches, runs them, and self-corrects from the failures — errors don't mean the run has failed."}
      </div>
      <pre ref={preRef} onScroll={onScroll}>
        {log || (failed ? "(no output captured)" : "starting…")}
      </pre>
    </div>
  );
}
