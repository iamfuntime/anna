# Enabling and Choosing ANNA's Voice

ANNA can listen to voice notes and talk back. On Slack and Telegram she
transcribes inbound voice notes to text and, when you reply by voice, speaks her
answer back as a voice note. This guide covers turning it on, picking a voice,
and the knobs worth tuning.

Voice is on by default in the config schema, but it does nothing until you give
ANNA an OpenAI API key. The CLI transport is text-only and unaffected.

## What it uses

Voice is powered entirely by OpenAI, through a single API key:

- **Speech-to-text (inbound):** OpenAI **Whisper** (`whisper-1`). Slack and
  Telegram voice notes are downloaded, transcribed, and handed to ANNA as if you
  had typed them. Roughly $0.006 per minute of audio.
- **Text-to-speech (outbound):** OpenAI **tts-1**. When your last message on a
  conversation was a voice note, ANNA's reply is synthesized back to a voice
  note. Roughly 1.8¢ per ~200-word reply on `tts-1`.

Both directions read the same key: **`OPENAI_API_KEY`**.

## Adding the key

Pick either path, then restart the daemon.

**Option A — edit `.env` directly.** Add the key to ANNA's live secrets file
(`~/anna/.env`), which is `chmod 600`:

```
OPENAI_API_KEY=sk-...
```

**Option B — run the setup wizard.** `anna-setup --reconfigure` now includes an
optional OpenAI-key prompt. Answer it (or press Enter to keep the existing key).
The wizard updates the key in place and leaves every other `.env` value alone.

Then restart so the daemon picks it up:

```
systemctl --user restart anna
```

Get a key at <https://platform.openai.com/api-keys>.

If you skip the key, voice notes won't transcribe and TTS replies won't
synthesize — ANNA falls back to a polite text reply. Nothing else breaks.

## Choosing the voice

The TTS voice is a **fixed set of OpenAI built-in voices**. There is no voice
cloning and no audio upload — you pick one of OpenAI's named voices by string.

The original six (the standard set, and what most operators use):

| voice     | vibe |
|-----------|------|
| `alloy`   | neutral, balanced, the default — safe for anything |
| `echo`    | calm, measured, slightly warm male timbre |
| `fable`   | expressive and storytelling, British-leaning |
| `onyx`    | deep, authoritative, low male register |
| `nova`    | bright, friendly, upbeat female timbre |
| `shimmer` | soft, gentle, soothing female timbre |

OpenAI has since added more voices to `tts-1` (`ash`, `coral`, `sage`); any
valid OpenAI tts-1 voice name works in the config below. The six above are the
canonical set and a good place to start.

Hear samples on OpenAI's text-to-speech docs and playground:
<https://platform.openai.com/docs/guides/text-to-speech>.

**To set or change the voice**, edit the `voice:` block in `~/anna/anna.yaml`:

```yaml
voice:
  outbound:
    voice_id: nova
```

Then restart (`systemctl --user restart anna`). There is no hot-reload, so a
voice change is not live until the daemon restarts.

## Config knobs worth tuning

All of these live in the `voice:` block of `~/anna/anna.yaml`. The annotated
defaults are in `anna.yaml.example`.

- **`voice.outbound.voice_only`** (default `true`) — when a conversation's last
  inbound was a voice note, reply with voice *only* (no text echo). Set `false`
  to always include the text alongside the voice note. On Slack, where voice-note
  rendering is poor, the text echo is always posted regardless.
- **`voice.outbound.model`** (default `tts-1`) — `tts-1` is standard quality and
  cheapest. `tts-1-hd` is higher fidelity at roughly 2× the cost.
- **`voice.inbound.keep_audio_files`** (default `true`) — persist downloaded
  audio under `~/anna/transcripts/voice/` so you can re-listen or re-transcribe.
  Set `false` for zero persistence (the file is transcribed from a tempfile and
  deleted immediately).
- **Retention** — when `keep_audio_files` is on, saved audio is swept on the
  same schedule as transcripts (`logging.transcripts.retention_days`, default
  30 days). Lower it to keep less audio on disk.
- **`voice.inbound.enabled` / `voice.outbound.enabled`** — master on/off per
  direction if you want transcription without spoken replies, or vice versa.
- **`voice.outbound.transports`** (default `slack`, `telegram`) — which adapters
  may synthesize spoken replies.

## Restart required

ANNA has **no hot-reload**. Any change to `anna.yaml` or `.env` — the key, the
voice, any knob above — takes effect only after a daemon restart:

```
systemctl --user restart anna
```
