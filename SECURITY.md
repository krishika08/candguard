# Security Policy

## Supported Versions

CandGuard maintains security updates for the current published release line:

| Version | Supported          |
| ------- | ------------------ |
| 0.1.x   | :white_check_mark: |
| < 0.1.0 | :x:                |

The next release line, 0.2.x, is in development on the `main` branch and has not been published. Its support status will be added to this table when v0.2.0 is released. Until then, 0.1.x remains the supported release line.

## Reporting a Vulnerability

We take the security of CandGuard seriously. If you discover a security vulnerability, please do **not** disclose it in a public issue, pull request, or discussion.

### Preferred Reporting Method

Please report security vulnerabilities privately via GitHub Private Vulnerability Reporting:
- Navigate to the repository's **Security** tab.
- Click **Report a vulnerability** to open a draft security advisory.
- Alternatively, visit: `https://github.com/krishika08/candguard/security/advisories/new`

### Information to Include

To help us triage and resolve the issue quickly, please provide:
1. A description of the vulnerability and its potential impact.
2. Steps to reproduce the issue, including sample CSV/TSV or Parquet fixtures if applicable. Make sure no private or sensitive data is included.
3. The CandGuard version (or the commit, if you are using a development checkout), Python version, and operating system where the vulnerability was observed.
4. Any potential mitigations or suggested fixes.

### Response Process

- We will acknowledge receipt of your report within 48 hours.
- We will coordinate a fix in a private branch or fork.
- Once a fix is verified and ready for release, we will release a patch release and publish an advisory crediting the reporter (unless anonymity is requested).
