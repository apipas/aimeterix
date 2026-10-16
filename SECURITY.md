# Security policy

## Supported versions

AI Meterix is a 0.x preview. Security fixes go into the latest release only.

## Reporting a problem

Please report security problems privately through GitHub: open the repository's **Security** tab and choose **Report a vulnerability**. Do not open a public issue for them.

Say what you found, how to reproduce it, and which version you use. You will get an answer within a week. Once a fix is released, the report is published with credit to you unless you prefer otherwise.

## What AI Meterix promises

- **Nothing sensitive is stored.** Prompts, responses and API keys never reach the database, logs or buffers. The proxy keeps only counts from each response.
- **This machine only.** The proxy, API and dashboard listen on 127.0.0.1 and refuse other addresses. Default passwords must be changed before anything else works.
- **Not an open relay.** The proxy forwards only to the providers configured in the database, and can be limited to known API keys (`proxy.allowed_key_hashes`, stored as SHA-256 hashes).
- **No admin rights at run time.** No code path uses sudo; nothing is written outside its own config, data and logs folders (`aimeterix paths`) and the program folder. The `.pkg` and `.deb` installers need the admin password once to install; `install.sh` needs none. Release files are listed with their SHA-256 in `SHA256SUMS`, which `install.sh` checks.
- **Accounts.** Passwords are salted scrypt hashes and must be changed at first login. The dashboard uses an HttpOnly, SameSite=Strict session cookie, refuses changes sent with the cookie alone, and loads nothing from other sites.
- **Secrets.** A database password belongs in the environment or a `.env` file; one written into `aimeterix.yaml` is refused.
- **Few dependencies.** The core needs only `pydantic` and `pyyaml`; database drivers are optional extras.

Each promise has a test in `tests/unit/test_safety.py`, `test_api.py` or `test_config.py`.
