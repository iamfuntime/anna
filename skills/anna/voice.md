# Voice Messages

ANNA can take voice notes in and speak replies back, on Slack and Telegram. Inbound audio is transcribed (OpenAI Whisper) upstream of the router, so a voice note arrives as ordinary text. Outbound replies are optionally synthesized (OpenAI tts-1) back to a voice note. The CLI transport is text-only.

## How it reaches you

- A Slack or Telegram voice note is downloaded, transcribed, and dispatched exactly like a typed message — except the text is prefixed with a `[voice transcript]:` marker. That marker is the only mutation; treat the rest as normal text.
- When the most-recent inbound on a conversation was voice (within `voice.outbound.recent_voice_window_seconds`, default 600s), the reply is synthesized to a voice note. A text inbound after a voice inbound resets that, and the next reply goes back to text. This is the voice-in→voice-out, text-in→text-out default.

## Per-run tuning (when you see `[voice transcript]:`)

- Prefer brevity. The reply is spoken aloud, so a wall of text becomes a wall of speech.
- Drop formatting that does not survive TTS: no bullets, no headers, no code fences, no link syntax, no tables. Write plain, spoken-style prose.
- Read numbers, dates, and identifiers the way a person would say them. Spell out a short ID rather than dumping a raw token.
- Transcription is imperfect. If a transcript looks garbled or a key word is ambiguous, say what you think you heard and ask, rather than acting on a misheard instruction.
- Keep it to the answer. A voice reply that runs long is worse than a long text reply because the operator can't skim it.

## Operator-facing config (anna.yaml `voice:` block)

These are the operator's knobs, not yours — surface them only if asked:

- `voice.inbound.enabled` / `voice.outbound.enabled` — master on/off per direction.
- `voice.outbound.voice_id` — which OpenAI built-in voice speaks (alloy, echo, fable, onyx, nova, shimmer).
- `voice.outbound.voice_only` — voice-in produces voice-only-out; text-in still produces text-out.
- `voice.outbound.model` — `tts-1` (standard) or `tts-1-hd` (higher quality, 2× cost).
- `voice.inbound.keep_audio_files` — persist downloaded audio under `~/anna/transcripts/voice/` for re-listening, swept on the transcript retention schedule.

Config changes require a daemon restart (`systemctl --user restart anna`); there is no hot-reload. Full operator guide: `docs/voice.md`.

## Failure modes

- Voice inbound disabled but a voice note arrives → the operator gets a polite "voice transcription is off — type it" reply. Nothing for you to do.
- Audio over the size/duration cap, an unsupported codec, or a provider error → the operator sees a one-line failure instead of a silent drop. Respond to what you can see.
- Outbound TTS fails → the reply falls through to text-only automatically. No action needed.
