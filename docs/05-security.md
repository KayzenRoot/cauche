# Security

Owner bootstrap uses a locally supplied password and Argon2id. Sessions are opaque, hashed at rest, expiring, and revoked on password change. Cookie authentication uses HttpOnly, SameSite and Secure under HTTPS. Require origin and a per-session CSRF token for writes, and verify origin and session on event sockets. Throttle login using persisted state. No public administration or registration.

Encrypt venue credentials using a master credential outside PostgreSQL. Allowlist official demo destinations in code, disable redirects and validate broker account provenance. Never serialize secrets or upstream credential-bearing errors into logs. Keep local development on loopback. Require TLS for remote access.
