/**
 * The plan the Start a run dialog opens with: one task for the DevOps role of the dev-team
 * template, whose policy says `merge: ask`, so the run stops at a pending approval. It is the
 * `json first-approval` block of the quickstart guide, section 9, character for character (a test compares).
 */
export const FIRST_APPROVAL_PLAN = `{
  "data_class": "synthetic",
  "tasks": [
    {
      "ref": "deploy",
      "role": "devops",
      "title": "Deploy the fix to staging",
      "definition_of_done": "The deploy targets staging and a person approved it"
    }
  ]
}`;
