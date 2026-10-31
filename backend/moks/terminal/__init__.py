"""Web terminal: one disposable, isolated pod per session, reached through the moks-terminal gateway.

The gateway is its own component so that the API, worker and executor gain nothing. It is the only
holder of pods/exec in the terminal namespace, and it never sees production write credentials.

A shell is arbitrary code inside its sandbox. Nothing here filters commands: what a session can do is
decided by its IAM role, its Kubernetes RBAC and the namespace's NetworkPolicy.
"""
