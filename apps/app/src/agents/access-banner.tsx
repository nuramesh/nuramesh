import { NotAllowedError } from "@/api/client";

import { useErasedIssuerCredentials } from "./queries";

/**
 * Owners' banner (spec/agent-identity section 10, Q4): live credentials whose issuer has been
 * erased. Derived from the list, so it stays until every one is rotated or revoked. Approvers and
 * viewers get a refusal from the server and see nothing.
 */
export function AccessBanner({ agent }: { agent?: string }) {
  const { data: all, error } = useErasedIssuerCredentials();
  if (error instanceof NotAllowedError || all === undefined) return null;
  const data = agent === undefined ? all : all.filter((c) => c.agent === agent);
  if (data.length === 0) return null;
  const n = data.length;
  return (
    <section aria-label="Credentials without an issuer" className="rounded-md border border-warning/60 bg-card p-3 text-[13.5px]">
      <p>
        {n} agent {n === 1 ? "credential was" : "credentials were"} issued by a person who has been erased. Re-own {n === 1 ? "it" : "them"} (rotate) or revoke{" "}
        {n === 1 ? "it" : "them"}.
      </p>
      <ul className="mt-1 list-disc pl-5 text-xs text-muted-foreground">
        {data.map((c) => (
          <li key={c.id}>
            <a href={`#access-${c.agent}`} className="underline">
              {c.agent}
            </a>{" "}
            <code>{c.id}</code>
          </li>
        ))}
      </ul>
    </section>
  );
}
