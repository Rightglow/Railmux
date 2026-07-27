import { useState } from "react";

export default function CopyCommand({ command }: { command: string }) {
  const [copied, setCopied] = useState(false);

  async function copy() {
    await navigator.clipboard.writeText(command);
    setCopied(true);
    window.setTimeout(() => setCopied(false), 1600);
  }

  return (
    <div className="copy-command">
      <span aria-hidden="true">$</span>
      <code>{command}</code>
      <button type="button" onClick={copy} aria-label={`Copy ${command}`}>
        {copied ? "Copied" : "Copy"}
      </button>
    </div>
  );
}
