# Security and private data

Do not put credentials, benchmark prompts, answer keys, raw responses, SQLite databases or private logs into public issues or pull requests. Use GitHub's private vulnerability reporting when available; otherwise contact the repository owner privately with a minimal description and no secrets.

The public site contains aggregate scores, model settings and sanitized manifests only. Local state has restrictive permissions and stays outside the repository. Credentials are read from macOS Keychain or the documented local/environment sources. GitHub Actions performs offline checks and publication with no inference or dataset credentials.

Generated Python is untrusted. Run the pinned Linux grader only through the configured disposable Docker containers. Do not run generated answers or deserialize benchmark test pickles on the host. Docker isolation is not a guarantee against kernel vulnerabilities; keep the local engine and host patched.

Before publishing a change, run the production/privacy checks and validate the staged public artifact. Changes to model routing, usage accounting, tools, prompts or graders require compatibility review and, where settings change, new evaluation epochs or seasons. Preserve old records.
