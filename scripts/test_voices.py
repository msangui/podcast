"""
Quick voice preview — generates a short test clip for each host using the current
voice settings from config/show-format.json and voice IDs from .env.

Usage:
  python3 scripts/test_voices.py              # test both hosts
  python3 scripts/test_voices.py claire         # test Hans only
  python3 scripts/test_voices.py flint        # test Flint only

Output: assets/test-claire.mp3 and/or assets/test-flint.mp3

Iterate fast: edit config/show-format.json → run this script → listen → repeat.
No GitHub push, no n8n restart needed.
"""
import json, sys, os, urllib.request, urllib.error

# ── Test sentences (chosen to exercise each character's personality) ──────────
TEST_LINES = {
    "CLAIRE": "Hello, I'm Claire. I've been studying artificial general intelligence "
             "for over a decade, and I can tell you — this week's news is actually "
             "worth paying attention to.",
    "FLINT": "Alright folks, buckle up — today in AI we have got breakthroughs, "
             "breakdowns, and at least one billionaire saying something that made me "
             "spit out my coffee!"
}

def load_env(path="./.env"):
    env = {}
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                env[k.strip()] = v.strip()
    return env

def load_show_format(path="./config/show-format.json"):
    with open(path) as f:
        return json.load(f)

def call_elevenlabs(voice_id, text, voice_settings, api_key):
    payload = json.dumps({
        "text": text,
        "model_id": "eleven_multilingual_v2",
        "voice_settings": voice_settings
    }).encode()
    req = urllib.request.Request(
        f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}",
        data=payload,
        headers={
            "xi-api-key":   api_key,
            "Content-Type": "application/json",
            "Accept":       "audio/mpeg"
        },
        method="POST"
    )
    try:
        with urllib.request.urlopen(req) as r:
            return r.read()
    except urllib.error.HTTPError as e:
        body = e.read().decode()
        raise RuntimeError(f"ElevenLabs HTTP {e.code}: {body}")

def main():
    filter_host = sys.argv[1].upper() if len(sys.argv) > 1 else None

    env         = load_env()
    show_format = load_show_format()
    api_key     = env["ELEVENLABS_API_KEY"]
    os.makedirs("assets", exist_ok=True)

    hosts = {
        "CLAIRE":  {
            "voice_id": env.get("ELEVENLABS_CLAIRE_VOICE_ID")
                        or show_format["hosts"]["host_1"]["voice_id"],
            "settings": show_format["hosts"]["host_1"].get("voice_settings",
                        {"stability": 0.28, "similarity_boost": 0.75,
                         "style": 0.40, "use_speaker_boost": True}),
            "out": "assets/test-claire.mp3"
        },
        "FLINT": {
            "voice_id": env.get("ELEVENLABS_FLINT_VOICE_ID")
                        or show_format["hosts"]["host_2"]["voice_id"],
            "settings": show_format["hosts"]["host_2"].get("voice_settings",
                        {"stability": 0.45, "similarity_boost": 0.75,
                         "style": 0.20, "use_speaker_boost": True}),
            "out": "assets/test-flint.mp3"
        }
    }

    for name, cfg in hosts.items():
        if filter_host and filter_host != name:
            continue
        s = cfg["settings"]
        label = (f"stability={s.get('stability')}, "
                 f"style={s.get('style', 0)}, "
                 f"similarity={s.get('similarity_boost')}")
        print(f"→ Testing {name:<5} (voice: {cfg['voice_id']}, {label}) ... ", end="", flush=True)
        audio = call_elevenlabs(cfg["voice_id"], TEST_LINES[name], cfg["settings"], api_key)
        with open(cfg["out"], "wb") as f:
            f.write(audio)
        print(f"✓  saved to {cfg['out']}  ({len(audio):,} bytes)")

if __name__ == "__main__":
    main()
