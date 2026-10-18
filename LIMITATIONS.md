# WorkGrid — Architectural Limitations & Day-One Boundaries

> **Phase 3 Status**: This document records known system limitations, deliberate non-goals, and boundary conditions as outlined in the WorkGrid Constitution.
>
> We distinguish clearly between:
> 1. **Currently Verified Boundaries (Phases 0–3)**: Capabilities and boundary conditions verified in the active codebase.
> 2. **Planned Architectural Boundaries (Future Phases)**: Inherent architectural constraints and deliberate design trade-offs defined for upcoming phases.

---

## 1. Currently Verified Boundaries (Phases 0–3)

- **Application Scope**: Phases 0–3 are implemented. The current PostgreSQL-backed automated test suite must be run from `backend/` to establish its present pass count; this audit adds regression coverage for concurrency and rollback paths.
- **Authentication**: JWT-based login with Argon2id password hashing. Development identity headers are restricted to explicitly configured development/test environments; production is the fail-closed default and requires a strong JWT secret. Multi-Factor Authentication (MFA), SSO (SAML/OAuth2), and session revocation lists are intentionally not implemented.
- **Notification Delivery**: In-app only. Notifications are written to a `notifications` table by the outbox worker and read via polling API (`GET /notifications`, `GET /notifications/unread-count`). The frontend polls unread count every 15 seconds. Server-Sent Events (SSE) for real-time push are deferred to Phase 5.
- **Outbox Worker Guarantees**: The worker provides at-least-once delivery with database-level deduplication. It does not guarantee strict ordering across events. Events are processed in claim order, which may differ from insertion order under concurrent worker scenarios.
- **Worker Scaling**: A single worker process handles the current scale. Multiple workers can run concurrently without double-processing (via `SKIP LOCKED`), but worker coordination, health monitoring, and auto-restart are not built into the application — they are expected to be managed by the OS process supervisor (systemd, Docker, etc.).
- **Outbox Table Growth**: Completed and dead-lettered outbox records are retained indefinitely. No automated pruning or archival job exists. At scale, old records would need periodic cleanup.
- **Append-Only Privilege Limitation**: Immutability on `role_assignment_audit` is enforced via a PostgreSQL trigger (`trg_role_assignment_audit_immutable`), which prevents modifications through application pathways. Table owners or database superusers can still disable triggers; production compliance will require database role privileges restricted to `INSERT, SELECT` and external WAL log replication.
- **Local Environment**: Verified on Python 3.11.4 and PostgreSQL 17 on Windows, with complete compatibility for Python 3.12+ and PostgreSQL 16 on Docker Compose.

---

## 2. Planned Architectural Boundaries (Future Phases)

The following boundaries were intentionally chosen to preserve system simplicity, predictability, and defensibility during development:

### 2.1 Access Control & Identity
- **Basic Authentication**: Version 1 uses email/password authentication with Argon2id and PyJWT. Multi-Factor Authentication (MFA), SSO (SAML/OAuth2), and session revocation lists are intentionally not implemented.
- **Deferred Role-Request Workflow**: Role requests are evaluated in a single step by a designated approver from an approval policy table or an organization owner. Multi-tier approval chains, request expiration timers, and custom delegated approver policies are excluded.
- **Scope Hierarchy Depth**: Scopes are strictly structured in three tiers: Organization $\rightarrow$ Department $\rightarrow$ Team. Arbitrary multi-nested team hierarchies are not supported.

### 2.2 Notifications & Delivery
- **In-App Delivery Only**: Notifications are written to an internal `notifications` table and delivered via polling (Phase 3) or Server-Sent Events (Phase 5). External communication integrations (email via SMTP/SendGrid, Slack webhooks, SMS) are deliberately omitted.
- **SSE Fan-Out per Process (Phase 5)**: Live updates will be driven by PostgreSQL `LISTEN`/`NOTIFY` piped into SSE streams. A single process handles hundreds of concurrent SSE connections comfortably, but broad fan-out to tens of thousands of simultaneous clients on a single node is bounded by connection file descriptors and thread pools.
- **No Notification Preferences**: Users cannot currently configure which notification types they receive. All events generate notifications for all relevant recipients.

### 2.3 Search & Pagination
- **Non-Typo-Tolerant Search**: Item search relies on PostgreSQL's built-in full-text search (`tsvector` / `to_tsquery`) with a GIN index on title and description. Typo tolerance, fuzzy matching (e.g., Levenshtein distance), and external search engines (Elasticsearch, Meilisearch) are intentionally omitted.
- **Recency-First Keyset Pagination**: Keyset pagination orders records strictly by `(updated_at DESC, id DESC)` with opaque cursors. Search relevance ranking is traded off to maintain deterministic, drift-free pagination cursors.

### 2.4 Scalability & Storage
- **Single-Region, Single Database**: The architecture assumes a primary PostgreSQL instance with synchronous read-after-write consistency. Cross-region multi-master replication is not supported.
- **Summary Count Query Scans**: Status and priority summary counts execute aggregate queries scoped to a team. At massive scale (tens of millions of items per team), this will require moving from aggregate scans to trigger-maintained tally counters.
- **No File Attachments or Unstructured Documents**: Work items maintain text descriptions and activity comments. File uploads, binary storage (S3/MinIO), and rich document viewers are out of scope.

---

## 3. Day-One Operational Questions for Newtonite

Before expanding beyond Phase 0 and rolling out into production, the following organizational parameters must be confirmed:
1. **Team & Department Topology**: How many organizations, departments, and teams must be seeded on day one?
2. **Approval Authority Mapping**: Who holds the default owner role, and which team leader roles have authority to decide cross-department requests?
3. **Current Inflow Channels**: Where do operational incidents and work items currently originate (spreadsheets, email, chat threads), and what format must data import follow?
4. **Volume Expectations**: What is the target rate of work items created per day and the target retention window for closed items?
