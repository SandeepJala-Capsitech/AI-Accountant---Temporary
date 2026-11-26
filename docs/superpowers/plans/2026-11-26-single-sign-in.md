# Single Sign-In — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** One username and password, set in the environment, guard every API route, so the app can go on the internet (frontend on Vercel, API on an always-on host) without exposing clients' books.

**Architecture:** A new `ledgersync/auth.py` checks the credentials, issues and verifies a signed session token (HMAC-SHA256, standard library only) and throttles failed sign-ins. `server.py` gains a middleware that answers 401 for any `/api/*` request without a valid `ledgersync_session` cookie, plus `/api/auth/login`, `/api/auth/logout` and `/api/auth/session`. The browser only ever calls same-origin `/api/...` (Vercel's rewrite forwards it), so the API sets the cookie as HttpOnly on the Vercel domain. The frontend gains a `/login` page, sends a person there on any `not_signed_in` 401, and shows Sign out in the header.

**Tech Stack:** Python 3.11 / FastAPI / pytest (`.venv/bin/python -m pytest <files> -q -p no:cacheprovider`); Next.js 16 + TypeScript (`cd frontend && npm test && npx tsc --noEmit`).

**Spec:** none written. The decisions agreed in the session that produced this plan are in Global Constraints below.

## Global Constraints

- Sign-in is on only when both `LEDGERSYNC_AUTH_USERNAME` and `LEDGERSYNC_AUTH_PASSWORD` are set. With neither set it is off, so local development and the existing tests are unchanged. With only one of them set, `create_app` refuses to start.
- With sign-in on, `LEDGERSYNC_AUTH_SECRET` must be at least 32 characters, or `create_app` refuses to start. Generate it with `python -c "import secrets; print(secrets.token_urlsafe(48))"`.
- `LEDGERSYNC_SESSION_DAYS` (default `7`) sets how long a session lasts.
- Cookie: name `ledgersync_session`, `HttpOnly`, `SameSite=Lax`, `Path=/`, `Max-Age` = days × 86400. It is `Secure` when `X-Forwarded-Proto` (or the request scheme) is `https`. Never set a `Domain`.
- Token: `"<expires_unix_int>.<hex hmac>"`. The HMAC key is the secret, and the message is `f"{username}\n{password}\n{expires}"`, so changing the username or password signs everyone out.
- Public paths with sign-in on: `/api/health`, `/api/auth/login`, `/api/auth/logout`, `/api/auth/session`, plus every `OPTIONS` request. Everything else under `/api/` needs the cookie. Paths outside `/api/` are not gated (the API serves none).
- Comparisons use `hmac.compare_digest` on UTF-8 bytes, for both credentials and the signature.
- Throttle: after 10 failed sign-ins within 900 seconds, every sign-in answers 429 until the oldest failure is more than 900 s old. A success clears the count. It is global, not per IP, because behind Vercel every request comes from Vercel's addresses.
- Error bodies use the existing shape `{"detail": {"code", "message"}}`, with these exact codes and messages:
  - `not_signed_in` (401): "Sign in to use Super Accountant."
  - `wrong_credentials` (401): "That username and password don't match."
  - `too_many_attempts` (429): "Too many wrong attempts. Wait 15 minutes and try again."
- `/api/auth/login` requires the `X-LedgerSync` header like the other writes (`RefusedRequest`, 403, when missing).
- When sign-in is off, `create_app` logs a WARNING: "Sign-in is off: set LEDGERSYNC_AUTH_USERNAME and LEDGERSYNC_AUTH_PASSWORD before this API is reachable from the internet."
- No new Python or npm dependencies.
- The password, secret and token are never logged or printed (`field(repr=False)`, as for the API keys).
- Commit only this plan's files. `docs/superpowers/issues/2026-11-24-agency-run-issues.md` is someone else's uncommitted work.

## Review Focus

1. **The login page itself gets a 401 loop:** a wrong password answers `wrong_credentials`, not `not_signed_in`, so the page shows the message and never redirects. Test in Task 3.
2. **Open redirect through `?next=`:** `next=https://evil.example` or `next=//evil.example` must land on `/`. Test in Task 3.
3. **A token whose expiry part someone edited** (a later timestamp, the same signature) is refused. Test in Task 1.
4. **The cookie is Secure only behind HTTPS:** `X-Forwarded-Proto: https` gives `Secure`, and plain local http doesn't. Otherwise local sign-in would break. Test in Task 2.
5. **Uploads and the chat stream work once signed in.** `/api/analyze` and the SSE `POST /api/chat/threads/{id}/messages` go through the gate like any route, and a request without the cookie gets 401 before the body is read. Test in Task 2 (analyze with no cookie → 401).

---

### Task 1: Settings and the auth module

**Files:**
- Modify: `ledgersync/config.py` (add four names to `ENV_NAMES`, four fields, `from_env` reads, an `auth_required` property)
- Modify: `ledgersync/errors.py` (three error classes)
- Modify: `.env.example` (four commented defaults, next to `LEDGERSYNC_BUSINESS_NAME`)
- Create: `ledgersync/auth.py`
- Test: `tests/test_config.py`, `tests/test_auth.py` (new)

**Interfaces:**
- Produces, in `Settings`: `auth_username: str = ""`, `auth_password: str = field(default="", repr=False)`, `auth_secret: str = field(default="", repr=False)`, `session_days: int = 7`, and the property `auth_required -> bool` (both username and password non-empty). They are read from `LEDGERSYNC_AUTH_USERNAME`, `LEDGERSYNC_AUTH_PASSWORD`, `LEDGERSYNC_AUTH_SECRET` and `LEDGERSYNC_SESSION_DAYS`, with the first three stripped.
- Produces, in `ledgersync/errors.py`: `NotSignedIn` (401, `not_signed_in`), `WrongCredentials` (401, `wrong_credentials`), `TooManyAttempts` (429, `too_many_attempts`).
- Produces, in `ledgersync/auth.py`:
  - `COOKIE = "ledgersync_session"`
  - `PUBLIC_PATHS: frozenset[str]` (the four paths in Global Constraints)
  - `check_settings(settings: Settings) -> None`: raises `ValueError` with a message naming the missing variable when exactly one of username or password is set, or when sign-in is on and the secret is under 32 characters
  - `credentials_match(settings: Settings, username: str, password: str) -> bool`: False when sign-in is off
  - `make_token(settings: Settings, now: float) -> str`
  - `token_valid(settings: Settings, token: Optional[str], now: float) -> bool`
  - `class LoginThrottle(limit: int = 10, window: float = 900.0, clock: Callable[[], float] = time.monotonic)` with `allowed() -> bool`, `failed() -> None` and `succeeded() -> None`. It is thread-safe (`threading.Lock`).

- [ ] **Step 1: Write the failing tests**

In `tests/test_config.py`:

```python
def test_sign_in_is_off_by_default_and_on_with_both_credentials():
    assert Settings.from_env({}).auth_required is False
    s = Settings.from_env({"LEDGERSYNC_AUTH_USERNAME": " sandeep ", "LEDGERSYNC_AUTH_PASSWORD": "pw",
                           "LEDGERSYNC_AUTH_SECRET": "x" * 32, "LEDGERSYNC_SESSION_DAYS": "3"})
    assert s.auth_required and s.auth_username == "sandeep" and s.session_days == 3
    assert "pw" not in repr(s) and "x" * 32 not in repr(s)
```

`tests/test_auth.py`, using a helper `on(**kw)` that returns `Settings(auth_username="admin", auth_password="s3cret", auth_secret="k" * 32, **kw)`:

- `test_check_settings_refuses_half_set_credentials`: `Settings(auth_username="admin")` raises `ValueError` matching `LEDGERSYNC_AUTH_PASSWORD`. `Settings(auth_password="pw")` raises one matching `LEDGERSYNC_AUTH_USERNAME`.
- `test_check_settings_refuses_a_short_secret`: `on()` with `auth_secret="short"` raises one matching `LEDGERSYNC_AUTH_SECRET`. `check_settings(Settings())` returns None.
- `test_credentials_match_only_the_exact_pair`: True for `("admin", "s3cret")`. False for `("admin", "S3cret")`, `("Admin", "s3cret")` and `("", "")`. False for anything when sign-in is off.
- `test_a_fresh_token_is_valid_until_it_expires`: `t = make_token(on(), now=1000.0)`. `token_valid(on(), t, 1000.0 + 7*86400 - 1)` is True, and at `1000.0 + 7*86400 + 1` it is False.
- `test_an_edited_expiry_is_refused`: take `t`, replace the part before `.` with a value 10 days later, and expect False.
- `test_changing_the_password_signs_everyone_out`: a token from `on()` is invalid under `on()` with `auth_password="new"`.
- `test_rubbish_tokens_are_refused`: False for `None`, `""`, `"abc"`, `"1.2.3"` and `"notanumber.ff"`.
- `test_throttle_blocks_after_ten_failures_and_reopens_after_the_window`: with a fake clock, 10 × `failed()` makes `allowed()` False. Move the clock 901 s and it is True. `succeeded()` after 9 failures resets the count.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_auth.py tests/test_config.py -q -p no:cacheprovider`
Expected: FAIL (`ledgersync.auth` not found, `auth_required` missing).

- [ ] **Step 3: Implement the settings, errors and `ledgersync/auth.py`**

The throttle keeps a `collections.deque` of failure times and drops those older than `window` on each call. `token_valid` splits on the first `.`, parses the expiry as `int`, recomputes the HMAC and compares it with `hmac.compare_digest`, then checks `now < expires`. In `from_env`, read the three strings with `env.get("…", "")` and the days with `_number(env, "LEDGERSYNC_SESSION_DAYS", d.session_days)`: `test_env_names_are_the_variables_from_env_reads` finds names only in those two forms, and blank values must give the defaults. Add the four variables to `.env.example` as commented defaults (`# LEDGERSYNC_AUTH_USERNAME=` and so on, `# LEDGERSYNC_SESSION_DAYS=7`), with a one-line comment above them on how to generate the secret. `test_the_defaults_in_env_example_are_the_defaults` must keep passing.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_auth.py tests/test_config.py -q -p no:cacheprovider`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add ledgersync/auth.py ledgersync/config.py ledgersync/errors.py .env.example tests/test_auth.py tests/test_config.py
git commit -m "feat: one username and password, a signed session token and a sign-in throttle"
```

---

### Task 2: The API gate and the sign-in routes

**Files:**
- Modify: `server.py`: in `create_app`, call `auth.check_settings(settings)` before the store is opened and log the off warning. Add the gate middleware after `app.add_middleware(ReadsClientOnce)`, and the three routes in a new `# ─── Sign-in ───` section under Health.
- Modify: `README.md`: add the four variables to the Configuration table, and add a `## Deploying` section after Configuration (content in Step 3)
- Test: `tests/test_auth_api.py` (new; no MongoDB needed)

**Interfaces:**
- Consumes: everything Task 1 produces.
- Produces these HTTP routes:
  - `POST /api/auth/login` takes JSON `{"username": str, "password": str}` and needs `X-LedgerSync`. It answers 204 with `Set-Cookie`, 401 `wrong_credentials`, or 429 `too_many_attempts`. The throttle is checked before the credentials.
  - `POST /api/auth/logout` answers 204 and clears the cookie (`Max-Age=0`).
  - `GET /api/auth/session` answers `{"auth_required": bool, "signed_in": bool}`, where `signed_in` is True whenever sign-in is off.
  - Any other `/api/*` request without a valid cookie gets 401 `not_signed_in` while sign-in is on.

- [ ] **Step 1: Write the failing tests**

`tests/test_auth_api.py` builds the app without MongoDB, as `tests/test_api.py` does: `create_app(settings, model_client=FakeModel(), store=SpyStore(), blobs=object())` with `Settings(warmup=False, auth_username="admin", auth_password="s3cret", auth_secret="k"*32)`, wrapped in `TestClient(app, headers={"X-LedgerSync": "1"})`. Copy `SpyStore` or import it from `test_api`. `GET /api/accounts` without `client_id` stands in for a protected route, because it never touches the store.

- `test_without_a_cookie_every_api_route_is_401`: `GET /api/accounts` and `POST /api/analyze` (text `"x"`) both answer 401 with `detail.code == "not_signed_in"`.
- `test_health_and_session_stay_public`: `GET /api/health` is 200. `GET /api/auth/session` equals `{"auth_required": True, "signed_in": False}`.
- `test_signing_in_opens_the_api`: login with the right pair answers 204. Then `GET /api/accounts` is 200, and `/api/auth/session` reports `signed_in` True.
- `test_the_cookie_is_httponly_lax_and_only_secure_behind_https`: the plain login's `set-cookie` contains `HttpOnly`, `SameSite=lax` (case-insensitive) and `Max-Age=604800`, and not `Secure`. The same login sent with header `X-Forwarded-Proto: https` contains `Secure`.
- `test_a_wrong_password_is_401_wrong_credentials`: the code is `wrong_credentials`, and no `set-cookie` is sent.
- `test_the_eleventh_attempt_is_throttled`: 10 wrong logins, then the right one, answers 429 `too_many_attempts`.
- `test_login_needs_the_pages_header`: a client without `X-LedgerSync` gets 403 `refused_request`.
- `test_signing_out_closes_the_api`: login, logout (204), then `GET /api/accounts` is 401.
- `test_with_sign_in_off_nothing_changes`: with plain `Settings(warmup=False)`, `GET /api/accounts` is 200 with no cookie, and the session answers `{"auth_required": False, "signed_in": True}`.
- `test_half_set_credentials_refuse_to_start`: `create_app(Settings(warmup=False, auth_username="admin"), ...)` raises `ValueError`.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_auth_api.py -q -p no:cacheprovider`
Expected: FAIL (401 not returned, routes missing).

- [ ] **Step 3: Implement the gate, the routes and the README section**

The gate is an `@app.middleware("http")` that lets the request through when sign-in is off, the method is `OPTIONS`, the path doesn't start with `/api/`, the path is in `PUBLIC_PATHS`, or `auth.token_valid(settings, request.cookies.get(auth.COOKIE), time.time())` holds. Otherwise it returns `JSONResponse(401, {"detail": {"code": "not_signed_in", "message": ...}})` directly: an exception raised inside `@app.middleware` doesn't reach the `LedgerSyncError` handler. Create one `LoginThrottle` per app. The login route raises `TooManyAttempts` or `WrongCredentials` and is otherwise handled by the existing exception handler. Check its header inline with the same `RefusedRequest` message pattern as `from_our_pages`, which is defined further down.

README `## Deploying` covers these points, in this order:
1. MongoDB Atlas: reuse `MONGODB_URI`, and add the API host's outbound IPs in Network Access.
2. API on an always-on host with one instance and one worker (jobs live in memory). Start command: `uvicorn server:create_app --factory --host 0.0.0.0 --port $PORT --workers 1`. Health check: `/api/health`. Python 3.11. At least 1 GB of memory, or `LEDGERSYNC_MAX_PARALLEL_JOBS=2`.
3. The API's environment: the model keys, `MONGODB_URI`, `LEDGERSYNC_BUSINESS_NAME`, and the three `LEDGERSYNC_AUTH_*` variables.
4. Frontend on Vercel: Root Directory `frontend`, `API_URL` set to the API's URL, `LEDGERSYNC_MAX_UPLOAD_MB=20`. Redeploy after changing `API_URL`.
5. Vercel stops a forwarded request after 120 s. Document reads run as jobs, so they aren't affected.

- [ ] **Step 4: Run the tests to verify they pass, plus the existing API tests**

Run: `.venv/bin/python -m pytest tests/test_auth_api.py tests/test_api.py tests/test_config.py -q -p no:cacheprovider`
Expected: PASS. Tests that need MongoDB skip when `LEDGERSYNC_TEST_MONGODB_URI` isn't set.

- [ ] **Step 5: Commit**

```bash
git add server.py README.md tests/test_auth_api.py
git commit -m "feat: the API answers only a signed-in browser when a username and password are set"
```

---

### Task 3: The login page, the redirect and Sign out

**Files:**
- Create: `frontend/src/lib/auth.ts`
- Create: `frontend/src/lib/auth.test.ts`
- Modify: `frontend/src/lib/api.ts`: in `errorFrom`, after building the `ApiError`, call `signedOut()` when its code is `not_signed_in`
- Create: `frontend/src/app/login/page.tsx`
- Modify: `frontend/src/components/AppHeader.tsx`: a Sign out button when the session reports `auth_required && signed_in`

**Interfaces:**
- Consumes: Task 2's routes and error codes.
- Produces, in `auth.ts`:
  - `type Session = { auth_required: boolean; signed_in: boolean }`
  - `getSession(): Promise<Session>`
  - `signIn(username: string, password: string): Promise<void>`: POSTs JSON with `X-LedgerSync: 1` and `Content-Type: application/json`, and rejects with the `ApiError` from `errorFrom`
  - `signOut(): Promise<void>`
  - `loginPath(current: string): string`: gives `/login?next=<encodeURIComponent(current)>`
  - `safeNext(next: string | null): string`: returns `next` only when it starts with `/` and not with `//` or `/\`, otherwise `/`
  - `signedOut(): void`: when `typeof window !== 'undefined'` and the path isn't already `/login`, calls `window.location.assign(loginPath(location.pathname + location.search))`

Import `request` and `errorFrom` from `api.ts` in `auth.ts`. `api.ts` imports only `signedOut` from `auth.ts`. Both are ES modules and the import is used only at call time, so the cycle is safe. If `tsc` or `node --test` complains, move `signedOut` and `loginPath` into `api.ts` and re-export them from `auth.ts`.

- [ ] **Step 1: Write the failing tests** in `frontend/src/lib/auth.test.ts` (stub `globalThis.fetch` as `api.test.ts` does)

- `safeNext keeps same-site paths only`: `'/clients/3?x=1'` → itself. `null`, `''`, `'https://evil.example'`, `'//evil.example'` and `'/\\evil.example'` → `'/'`.
- `loginPath carries the page to come back to`: `loginPath('/clients/3?y=2')` → `'/login?next=%2Fclients%2F3%3Fy%3D2'`.
- `a wrong password is a wrong_credentials error, not a sign-out`: the stub answers 401 `{detail: {code: 'wrong_credentials', message: "That username and password don't match."}}`. `signIn` rejects with `code === 'wrong_credentials'` and that exact message.
- `signIn sends the pages header and the pair as JSON`: the stub records the init. Method is `POST`, headers include `X-LedgerSync: 1`, and the body parses to `{username: 'admin', password: 's3cret'}`.
- `an API call without a session rejects as not_signed_in`: the stub answers 401 `not_signed_in` for `request('/api/accounts')`, which rejects with `code === 'not_signed_in'`. There is no `window` in node, so `signedOut` does nothing here.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd frontend && npm test`
Expected: FAIL (`./auth.ts` not found).

- [ ] **Step 3: Implement `auth.ts`, the `errorFrom` hook, the login page and Sign out**

The login page is a client component with a form of two fields, `autoComplete="username"` and `"current-password"`, and a Sign in button that is disabled while sending. It shows the `ApiError` message under the form, and on success runs `window.location.assign(safeNext(new URLSearchParams(window.location.search).get('next')))`. Read `next` from `window.location.search` inside the submit handler rather than with `useSearchParams`, which in Next 16 needs a Suspense boundary on a static page. Check `frontend/node_modules/next/dist/docs/01-app` before writing the page, as `frontend/AGENTS.md` asks. Use the existing `globals.css` classes that the client dialog's form uses. The header loads `getSession()` once on mount. Sign out calls `signOut()` and then `window.location.assign('/login')`.

- [ ] **Step 4: Run the tests and type check to verify they pass**

Run: `cd frontend && npm test && npx tsc --noEmit && API_URL=http://127.0.0.1:8085 npx next build`
Expected: all tests pass, no type errors, and the build lists a `/login` route.

- [ ] **Step 5: Check it in the browser**

Start the API locally with the three `LEDGERSYNC_AUTH_*` variables set, and start the frontend dev server. Then check:
- Opening `/clients/1` lands on `/login?next=%2Fclients%2F1`.
- A wrong password shows the message and stays on the page.
- The right pair returns to `/clients/1` with its data.
- Uploading a document works.
- Sign out returns to `/login`.

- [ ] **Step 6: Commit**

```bash
git add frontend/src/lib/auth.ts frontend/src/lib/auth.test.ts frontend/src/lib/api.ts frontend/src/app/login/page.tsx frontend/src/components/AppHeader.tsx
git commit -m "feat: a sign-in page, back to the page you were on, and Sign out in the header"
```

---

### Final check

- [ ] Run the whole Python suite in the background: `.venv/bin/python -m pytest -q -p no:cacheprovider`. Expected: PASS, with the same skips as before this plan.
- [ ] Run `cd frontend && npm test && npx tsc --noEmit`. Expected: PASS.
