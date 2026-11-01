# Model provider

Step 1 of the setup wizard. Pick Meta (default, recommended) or a custom OpenAI-compatible endpoint. The choice works with both board backends.

## Meta (default)

Uses META_MODEL, META_BASE_URL, and META_API_KEY from your secrets file through the Responses API. Tool calling, structured output, and image input all work. Keep the Standard tier for real team text; Contributor may train on inputs and outputs, so it is for development with fake data only.

Meta-only features: SAM 3.1 sticky-note segmentation (planning photos) and realtime meeting transcription (Listen page). Both need the Meta endpoint.

## Custom endpoint

Point Pam at any OpenAI-compatible server: base URL, model name, and API key. The key is stored only in the local data directory and never appears in logs or wizard progress. Chat, tools, structured output, and image input work when the endpoint supports them. The wizard Test button checks all four in plain words.

Limits with a custom endpoint: the planner skips SAM segmentation and reads the whole photo as one image, and realtime transcription is unavailable. Everything else (interviews, synthesis, triage titles, notes) runs on the custom model.

## Switching

Change the provider any time on the Setup page. The choice is saved per project. Past activity and board writes are unchanged; only new model calls use the new provider.
