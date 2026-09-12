# Executor selection

| Route | Criteria | Examples |
| --- | --- | --- |
| LOCAL | Exact spec, one module, reversible, low ambiguity, trivial to verify | Docs draft, repetitive mapping |
| LOCAL + CLOUD REVIEW | Bounded implementation, approved contracts, automated checks | UI, CRUD, DTO, API adapter, approved migration |
| CLOUD | Ambiguity, cross-module impact, difficult debugging, weak testability | Requirements, decomposition, integration review |
| CLOUD ONLY | Architecture/shared contracts/security/concurrency impact | Auth, payments, booking capacity, DB redesign |

Even LOCAL output requires lead approval before integration in this bootstrap.
Local never owns architecture. A small diff can still be security critical.
Architect owns task scope and canonical docs; reviewer owns acceptance; QA owns
independent test criteria. Root configs, lockfiles, schema and contracts require
a dedicated cloud task and explicit shared_paths_approval with reviewer and reason.
Permissions do not expand when a model asks for them. Escalate ambiguous requirements.
