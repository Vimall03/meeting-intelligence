# Meeting Intelligence

An assistant for online meetings that finds related Drive, Jira, and Confluence content and drafts Jira tasks for review. Live listening currently supports Google Meet; other platforms are planned.

Build the extension with `cd extension` and `npm run build`, then load `extension/.output/chrome-mv3` in Brave as an unpacked extension. See [extension setup](extension/README.md) for microphone permissions and checks.

## Jev routing

The live meeting stream sends each new cleaned transcript batch to
`typesafe/jev-1.13` through OpenRouter. Configure the backend before starting it:

```bash
cd backend
cp app/.env.example app/.env
# Set OPENROUTER_API_KEY in app/.env
uv run uvicorn app.main:app --reload --log-level info
```

Jev receives the latest 45 seconds as `recent_context` and only transcript text
added since its previous successful evaluation as `new_context`. `NONE` is kept
server-side. Completed search results and task suggestions are sent to the
extension side panel.

### Meeting results timeline

Each meeting WebSocket starts with a `timeline_snapshot` containing an empty
`items` array. The backend sends another snapshot whenever a task suggestion
or Google Drive search finds relevant files. Each snapshot has a `session_id`, increasing
`revision`, and newest-first `items` with stable IDs, sequence numbers, and UTC
timestamps. Task entries contain `title`, `description`, and `confidence`.
Search entries contain a source ID and label (`google_drive` / `Google Drive`),
query and files. Empty searches and search failures do not create timeline
entries. The history belongs to the current WebSocket session.
The extension keeps the last snapshot after capture stops while the user stays
in that Meet room, and clears it when the room ends or changes.

### Real-meeting Jev evaluation

`backend/tests/fixtures/jev_real_meeting.json` contains 46 scored decision
windows from a real meeting log, with the exact Jev request contexts, observed
probabilities, and manually reviewed Search and Task labels. It also records
actionable moments that appeared only in the clipped carry-in context and the
unfinished final request. The default report uses the observed log scores:

```bash
backend/.venv/bin/python backend/jev_eval.py
```

Run `backend/.venv/bin/python backend/jev_eval.py --live` only when the meeting
participants have authorized sending these transcript excerpts to OpenRouter.
The live run uses the current Jev instructions and the same 0.75 thresholds.
The baseline measures recall per decision window; repeated positive windows
may describe one evolving task, which the event service deduplicates later.

## Google Drive backend integration

The backend owns the Google Drive OAuth flow and stores the refresh token in
`backend/.data/google_drive_token.json` by default. The extension only needs to
start authorization once; access-token refreshes happen server-side.

Configure the values in `backend/app/.env` from
`backend/app/.env.example`, then start the backend. The extension-facing HTTP
endpoints are:

```text
GET  /api/integrations/google-drive/status
GET  /api/integrations/google-drive/auth/start
GET  /api/integrations/google-drive/oauth/callback
GET  /api/integrations/google-drive/files/search?query=...
GET  /api/integrations/google-drive/files/{file_id}
POST /api/integrations/google-drive/disconnect
```

The live meeting search path calls `GoogleDriveService` directly after Gemma
extracts structured search terms. Search results are ranked, limited to the top
five files, and included in the meeting timeline. The older `RELATED_FILES`
event remains available for clients that have not yet switched to snapshots.

In the extension side panel, choose **Connect Google Drive** once and approve
the Google consent screen. The backend keeps the refresh token; the extension
does not store Google credentials. During capture, matching files appear in the
**Meeting activity** with links that open in Google Drive.

## Atlassian Cloud search and task creation

Create an OAuth 2.0 (3LO) app in the [Atlassian developer console](https://developer.atlassian.com/console/myapps/). Add the Jira API with classic scopes `read:jira-work` and `write:jira-work`. Add the Confluence API with `search:confluence` if you also want page search. The authorization request includes `offline_access` for refresh tokens. Register `http://localhost:8000/api/integrations/atlassian/oauth/callback` as the callback URL. Set `ATLASSIAN_CLIENT_ID`, `ATLASSIAN_CLIENT_SECRET`, and `ATLASSIAN_SITE_URL=https://your-team.atlassian.net` in `backend/app/.env`, then restart the backend. The site URL selects one site if your account has access to several.

In the side panel, choose **Connect Atlassian** and approve access. The backend stores the rotating refresh token in `backend/.data/atlassian_token.json` with owner-only permissions. Meeting search terms are sent to the granted Jira and Confluence sources. Matching issues and pages appear as separate cards in the results timeline. Searches are live; the app does not build a local content index. A task suggestion has a **Create Jira task** action. The user chooses a Jira project, can edit the title and description, then confirms creation. Jira boards display issues according to their filters, so the destination is a project rather than a board. Nothing is created merely because a suggestion appears or someone says “create” in a meeting.

The local integration endpoints are:

```text
GET  /api/integrations/atlassian/status
GET  /api/integrations/atlassian/auth/start
GET  /api/integrations/atlassian/oauth/callback
GET  /api/integrations/atlassian/search?query=...
GET  /api/integrations/atlassian/jira/projects
POST /api/integrations/atlassian/jira/tasks
POST /api/integrations/atlassian/disconnect
```

### Natural meeting regression cases

`backend/tests/fixtures/jev_natural_meetings.json` covers indirect Drive,
Confluence, and Jira retrieval; speech split across chunks; soft commitments;
new task requests; negations; completed work; and unresolved references.
Replay the recorded scores or opt into fresh API calls:

```bash
backend/.venv/bin/python backend/jev_eval.py backend/tests/fixtures/jev_natural_meetings.json
backend/.venv/bin/python backend/jev_eval.py backend/tests/fixtures/jev_natural_meetings.json --live
```

Task drafts describe the underlying work. Administrative titles such as
“Create Jira ticket for database error” get one correction attempt before
being discarded. Distinct task requests can appear within 90 seconds; duplicate
trigger text and identical draft titles are suppressed. API failures in one
search provider do not terminate transcription. STT logs include audio RMS
and peak values to distinguish silent input from recognition failures.
