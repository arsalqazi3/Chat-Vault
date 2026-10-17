# Security policy

chat-vault handles people's private chat history, so security reports are very welcome.

## Reporting a vulnerability

Please **don't open a public issue** for security problems. Instead, report privately via
GitHub: go to the repository's **Security** tab → **Report a vulnerability**
(private vulnerability reporting).

Please include:
- what you found and where (file, endpoint, or setting)
- steps to reproduce, ideally with synthetic data
- what an attacker could do with it

**Never include real chat history, exports, tokens or secrets in a report**, yours or anyone
else's.

You can expect an acknowledgement within a week. Fixes are released as soon as they're ready,
and reporters are credited in the release notes unless they'd rather not be.

## Supported versions

Only the latest release receives security fixes.

## Scope

In scope:
- reading or modifying data through the MCP tools beyond what they're meant to do
- leaks of chat content, tokens or secrets (logs, errors, files, the extension bundle)
- remote mode: bypassing the single-user GitHub check, OAuth flaws, redirect allowlist bypasses
- the Docker image, the desktop extension, and the CI/supply chain

Out of scope:
- what an AI provider does with chat content you choose to send it
- attacks that need an already-compromised computer, GitHub account or AI account
- social engineering

## How chat-vault protects data

See the "Privacy and security" section of the [README](README.md), and the design decisions in
[PROGRESS.md](PROGRESS.md).
