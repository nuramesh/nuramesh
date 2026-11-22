import { Link } from "react-router";

/** AE-14: what an approval is, in five lines. Static: no data, no network. */
export function ApprovingHelpPage() {
  return (
    <div className="flex max-w-prose flex-col gap-4 px-6 py-5">
      <title>What am I approving? · NuraMesh</title>
      <h1 className="text-[21px] font-semibold">What am I approving?</h1>
      <ol className="m-0 flex list-decimal flex-col gap-2 pl-5 text-[14.5px]">
        <li>An agent asked to do one thing, and its team's rules say a person must decide.</li>
        <li>You see what it wants and why you were asked.</li>
        <li>Approve lets it do exactly that one thing, once.</li>
        <li>Reject tells it no, and the run goes on without that action.</li>
        <li>Your decision and your reason are recorded.</li>
      </ol>
      <Link to="/approvals" className="inline-flex min-h-11 items-center self-start text-link underline">
        Back to approvals
      </Link>
    </div>
  );
}
