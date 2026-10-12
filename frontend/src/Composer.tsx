import { useState, type KeyboardEvent, type ChangeEvent } from "react";


interface Props {
  disabled: boolean;
  sending: boolean;
  onSend: (text: string) => void;
  /** Canned questions for the selected ontology (quick-pick dropdown). */
  questions?: string[];
}

export function Composer({ disabled, sending, onSend, questions }: Props) {
  const [value, setValue] = useState("");

  const submit = (text: string) => {
    const t = text.trim();
    if (!t || disabled) return;
    onSend(t);
    setValue("");
  };

  const onKeyDown = (e: KeyboardEvent<HTMLInputElement>) => {
    if (e.key === "Enter") {
      e.preventDefault();
      submit(value);
    }
  };

  // Picking a canned question fills the box AND submits it immediately. The
  // select resets to its placeholder so the same question can be picked again.
  const onPickQuestion = (e: ChangeEvent<HTMLSelectElement>) => {
    const q = e.target.value;
    e.target.selectedIndex = 0; // reset to the "I'm feeling lazy!" label
    if (!q || disabled || sending) return;
    setValue(q);
    submit(q);
  };

  const hasQuestions = Boolean(questions && questions.length > 0);

  return (
    <footer className="composer-bar">
      <div className="composer">
        {hasQuestions && (
          <select
            className="lazy-picker"
            disabled={disabled || sending}
            defaultValue=""
            onChange={onPickQuestion}
            title="Pick a ready-made question"
            aria-label="I'm feeling lazy! Pick a ready-made question"
          >
            <option value="" disabled>
              I'm feeling lazy!
            </option>
            {questions!.map((q, i) => (
              <option key={i} value={q}>
                {q.length > 90 ? q.slice(0, 89) + "…" : q}
              </option>
            ))}
          </select>
        )}
        <input
          type="text"
          value={value}
          disabled={disabled}
          placeholder={
            disabled ? "Select an ontology, then ask a question…" : "Ask a question…"
          }
          onChange={(e) => setValue(e.target.value)}
          onKeyDown={onKeyDown}
        />
        <button onClick={() => submit(value)} disabled={disabled || sending || !value.trim()}>
          {sending ? "…" : "Send"}
        </button>
      </div>
    </footer>
  );
}
