import getpass, requests

key = getpass.getpass("ElevenLabs API key (hidden): ").strip()
H = {"xi-api-key": key}

# 1. Is the key valid, and what plan is it?
r = requests.get("https://api.elevenlabs.io/v1/user/subscription", headers=H)
print("SUBSCRIPTION:", r.status_code)
if r.ok:
    s = r.json()
    print("  tier:", s.get("tier"), "| status:", s.get("status"),
          "| used:", s.get("character_count"), "/", s.get("character_limit"))
else:
    print(" ", r.text[:300])

# 2. Which voices can this account use?
r = requests.get("https://api.elevenlabs.io/v1/voices", headers=H)
print("VOICES:", r.status_code)
if r.ok:
    for v in r.json()["voices"]:
        print(" ", v["name"], v["voice_id"], v.get("category"))

# 3. Real TTS request with the values the app now uses
VOICE_ID = "pNInz6obpgDQGcFmaJgB"
MODEL_ID = "eleven_flash_v2_5"
r = requests.post(
    f"https://api.elevenlabs.io/v1/text-to-speech/{VOICE_ID}",
    headers={**H, "Content-Type": "application/json"},
    json={"text": "Testing S.E.N.T.I.A.L.", "model_id": MODEL_ID},
)
print("TTS:", r.status_code, r.text[:500] if not r.ok else f"OK, {len(r.content)} bytes")