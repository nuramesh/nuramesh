import { Link } from "react-router";

import { AuthCard } from "@/auth/auth-card";

/** The one page for every bad link (wrong, used, expired, withdrawn): it never says which (spec A-P2, A-P3). */
export function LinkUnusable() {
  return (
    <AuthCard
      title="This link can't be used"
      intro="It may be wrong, already used or expired. You can ask for a new one."
      footer={
        <Link to="/forgot-password" className="underline underline-offset-2">
          Send me a new link
        </Link>
      }
    >
      <title>Link · NuraMesh</title>
    </AuthCard>
  );
}
