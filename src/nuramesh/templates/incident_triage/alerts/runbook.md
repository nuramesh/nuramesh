# Runbook: payments-api (sample, fictional)

- Error rate above 25% for 5 minutes or checkouts failing: SEV1. Page the on-call and the owner of the service.
- Exhausted database connections right after a deploy: roll back the deploy first.
- Rollback is a production change: a human approves it, always.
- After the incident: write a postmortem within one business day (timeline, impact, cause, action items).
