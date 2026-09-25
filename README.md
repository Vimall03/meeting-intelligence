## Resources I used so far

1. https://docs.astral.sh/uv/getting-started/installation/
2. https://fastapi.tiangolo.com/
3. https://github.com/SYSTRAN/faster-whisper/blob/master/README.md?utm_source=chatgpt.com
4. https://github.com/m-bain/whisperX

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
server-side; `SEARCH_EXISTING_WORK` and `SUGGEST_NEW_TASK` are sent to the
extension side panel.
