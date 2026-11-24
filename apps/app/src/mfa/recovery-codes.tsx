import { useEffect, useState } from "react";
import { flushSync } from "react-dom";
import { useBlocker } from "react-router";

import { Button } from "@/components/ui/button";

import { recoveryFileText } from "./recovery-file";

/**
 * S5: the ten recovery codes, shown once. They live in the props of this component and nowhere else.
 * Copy, download and print run only on a click. "Continue" needs the box ticked; leaving any other way asks first.
 */
export function RecoveryCodes({ codes, note, onContinue }: { codes: readonly string[]; note?: string; onContinue: () => void }) {
  const [ticked, setTicked] = useState(false);
  const [printing, setPrinting] = useState(false);
  const [copied, setCopied] = useState<"idle" | "done" | "failed">("idle");
  const blocker = useBlocker(() => !ticked);

  useEffect(() => {
    if (ticked) return;
    const warn = (event: BeforeUnloadEvent): void => {
      event.preventDefault();
    };
    globalThis.addEventListener("beforeunload", warn);
    return () => {
      globalThis.removeEventListener("beforeunload", warn);
    };
  }, [ticked]);

  const download = (): void => {
    const blob = new Blob([recoveryFileText(codes, new Date())], { type: "text/plain" });
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = "nuramesh-recovery-codes.txt";
    link.click();
    URL.revokeObjectURL(url);
  };
  const copy = (): void => {
    const clipboard = (globalThis.navigator as Navigator | undefined)?.clipboard;
    if (clipboard === undefined) {
      setCopied("failed");
      return;
    }
    clipboard.writeText(codes.join("\n")).then(
      () => { setCopied("done"); },
      () => { setCopied("failed"); },
    );
  };
  const print = (): void => {
    flushSync(() => { setPrinting(true); });
    globalThis.print();
    setPrinting(false);
  };

  return (
    <>
    <section aria-labelledby="mfa-codes-title" className="flex flex-col gap-3 print:hidden" data-testid="mfa-recovery-codes">
      <h3 id="mfa-codes-title" className="text-base font-semibold">Your recovery codes</h3>
      {note !== undefined && <p role="status" className="text-sm">{note}</p>}
      <p className="text-sm text-muted-foreground">
        They are shown once. Anyone who sees them can sign in as you. Do not show this screen on a shared or recorded screen. Paste them into your password manager, then close this page.
      </p>
      <ul className="grid grid-cols-1 gap-1 font-mono text-sm sm:grid-cols-2" data-testid="mfa-code-list">
        {codes.map((code) => (
          <li key={code}>{code}</li>
        ))}
      </ul>
      <div className="flex flex-wrap items-center gap-2">
        <Button variant="outline" className="min-h-11" onClick={download}>Download as text</Button>
        <Button variant="outline" className="min-h-11" onClick={print}>Print</Button>
        <Button variant="outline" className="min-h-11" onClick={copy}>Copy</Button>
      </div>
      <p className="text-[13px] text-muted-foreground">Delete the downloaded file after you import it.</p>
      <p role="status" aria-live="polite" className="text-[13px] text-muted-foreground">
        {copied === "done" ? "Copied." : copied === "failed" ? "Could not copy. Select the codes and copy them by hand." : ""}
      </p>
      <div className="flex min-h-11 items-center gap-3">
        <input id="mfa-codes-saved" type="checkbox" className="size-4" checked={ticked} onChange={(e) => { setTicked(e.target.checked); }} />
        <label htmlFor="mfa-codes-saved" className="text-sm font-medium">I saved these codes</label>
      </div>
      <Button className="min-h-11 self-start" disabled={!ticked} onClick={onContinue}>Continue</Button>
      {blocker.state === "blocked" && (
        <div role="alertdialog" aria-label="Leave without saving the codes?" className="callout-warning flex flex-col gap-2 px-3 py-2 text-sm">
          <p>If you leave now the codes are lost. You can make new ones in Settings &gt; Security.</p>
          <div className="flex gap-2">
            <Button className="min-h-11" onClick={() => { blocker.reset(); }}>Stay</Button>
            <Button variant="outline" className="min-h-11" onClick={() => { blocker.proceed(); }}>Leave and lose the codes</Button>
          </div>
        </div>
      )}
    </section>
    {printing && (
      <pre className="hidden print:block" data-testid="mfa-print-block">
        {recoveryFileText(codes, new Date())}
      </pre>
    )}
    </>
  );
}
