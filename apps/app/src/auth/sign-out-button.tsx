import { LogOut } from "lucide-react";
import { useState } from "react";

import { describeProblem } from "@/api/problems";
import { Button } from "@/components/ui/button";
import { Tip } from "@/components/ui/tooltip";

import { useAuth, useAuthState } from "./use-session";

/** Sign out, in the sidebar. Shown only where someone is signed in. */
export function SignOutButton({ tip = false }: { tip?: boolean }) {
  const auth = useAuth();
  const state = useAuthState();
  const [failure, setFailure] = useState<string | null>(null);
  if (auth === undefined || state.kind !== "signed-in") return null;
  return (
    <>
      <Tip label="Sign out" disabled={!tip}>
        <Button
          variant="ghost"
          size="icon"
          className="size-11"
          aria-label="Sign out"
          onClick={() => {
            setFailure(null);
            auth.logout().catch((error: unknown) => {
              setFailure(describeProblem(error));
            });
          }}
        >
          <LogOut aria-hidden="true" />
        </Button>
      </Tip>
      {failure !== null && (
        <span role="alert" className="max-w-40 text-xs text-destructive">
          Couldn't sign out. {failure}
        </span>
      )}
    </>
  );
}
