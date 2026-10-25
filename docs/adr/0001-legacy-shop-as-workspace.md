# Keep the legacy shop scope as the workspace boundary

The current `shop_id` already scopes every restaurant ledger and is present in the public CLI and storage contracts, so the foundation keeps it as the compatibility identifier for a single user's workspaces while adding explicit profile metadata. We will not replace it with a universal table or reinterpret historical ledger rows; future domains may add their own projections behind the same workspace boundary.

**Consequences:** existing databases and callers keep working. The Agent API now maps a configured Bearer key to a trusted principal/owner and checks workspace ownership; full multi-user account management and delegated permissions remain future work. Profile changes are metadata for new routing and must not silently reinterpret historical captures.
