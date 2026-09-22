import { useState, type KeyboardEvent } from "react";

interface Props {
  disabled: boolean;
  sending: boolean;
  onSend: (text: string) => void;
}

export function Composer({ disabled, sending, onSend }: Props) {
  const [value, setValue] = useState("");

  const submit = () => {
    const text = value.trim();
    if (!text || disabled) return;
    onSend(text);
    setValue("");
  };

  const onKeyDown = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      submit();
    }
  };

  return (
    <footer className="composer-bar">
      <div className="composer">
        <textarea
          value={value}
          disabled={disabled}
          placeholder={
            disabled ? "Select an ontology, then ask a question…" : "Ask a question…"
          }
          onChange={(e) => setValue(e.target.value)}
          onKeyDown={onKeyDown}
        />
        <button onClick={submit} disabled={disabled || sending || !value.trim()}>
          {sending ? "…" : "Send"}
        </button>
      </div>
    </footer>
  );
}
