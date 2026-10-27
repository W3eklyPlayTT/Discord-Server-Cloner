import requests
import time
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

BASE = "https://discord.com/api/v10"

def make_session(token: str) -> requests.Session:
    s = requests.Session()
    s.headers.update({"Authorization": token, "Content-Type": "application/json"})
    retry = Retry(
        total=5,
        backoff_factor=1.0,
        status_forcelist=[500, 502, 503, 504],
        allowed_methods=["GET", "POST", "DELETE"],
        raise_on_status=False,
    )
    s.mount("https://", HTTPAdapter(max_retries=retry))
    return s

def rate_safe(func, *args, **kwargs):
    kwargs.setdefault("timeout", (10, 30))
    while True:
        r = func(*args, **kwargs)
        if r.status_code == 429:
            wait = r.json().get("retry_after", 1.0)
            print(f"  [rate limit] sleeping {wait}s")
            time.sleep(wait)
            continue
        if not r.ok:
            print(f"  [error] {r.status_code} — {r.text}")
            r.raise_for_status()
        return r.json() if r.content else None

def get_roles(s, guild_id):
    return rate_safe(s.get, f"{BASE}/guilds/{guild_id}/roles")

def get_channels(s, guild_id):
    return rate_safe(s.get, f"{BASE}/guilds/{guild_id}/channels")

def post_role(s, guild_id, payload):
    return rate_safe(s.post, f"{BASE}/guilds/{guild_id}/roles", json=payload)

def post_channel(s, guild_id, payload):
    return rate_safe(s.post, f"{BASE}/guilds/{guild_id}/channels", json=payload)

def wipe_channels(s, guild_id):
    print("[*] Wiping target channels...")
    for ch in get_channels(s, guild_id):
        rate_safe(s.delete, f"{BASE}/channels/{ch['id']}")
        print(f"  - deleted › {ch['name']}")
        time.sleep(0.4)

def wipe_roles(s, guild_id):
    print("[*] Wiping target roles...")
    for role in get_roles(s, guild_id):
        if role["name"] == "@everyone" or role.get("managed", False):
            continue
        try:
            rate_safe(s.delete, f"{BASE}/guilds/{guild_id}/roles/{role['id']}")
            print(f"  - deleted › {role['name']}")
        except requests.exceptions.HTTPError as e:
            if e.response.status_code in (403, 400):
                print(f"  ~ skipped › {role['name']}")
            else:
                raise
        time.sleep(0.4)

def build_overwrites(ch, role_map, source_id, target_id):
    overwrites = []
    for ow in ch.get("permission_overwrites", []):
        ow_id = ow["id"]
        if ow["type"] == 0:  
            if ow_id == source_id:
                ow_id = target_id        
            elif ow_id in role_map:
                ow_id = role_map[ow_id]  
            else:
                continue                 
        overwrites.append({
            "id":    ow_id,
            "type":  ow["type"],
            "allow": ow["allow"],
            "deny":  ow["deny"],
        })
    return overwrites

def clone_server(source_id: str, target_id: str, token: str):
    s = make_session(token)

    wipe_channels(s, target_id)
    wipe_roles(s, target_id)

    print("[*] Cloning roles...")
    roles = sorted(get_roles(s, source_id), key=lambda r: r["position"])
    role_map: dict[str, str] = {}

    for role in roles:
        if role["name"] == "@everyone":
            continue
        try:
            new_role = post_role(s, target_id, {
                "name":        role["name"],
                "color":       role["color"],
                "hoist":       role["hoist"],
                "mentionable": role["mentionable"],
                "permissions": role["permissions"],
            })
            role_map[role["id"]] = new_role["id"]
            print(f"  + role  › {role['name']}")
        except requests.exceptions.HTTPError as e:
            if e.response.status_code in (400, 403):
                print(f"  ~ skipped role › {role['name']}")
            else:
                raise
        time.sleep(0.4)

    print("[*] Cloning channels...")
    channels = sorted(get_channels(s, source_id), key=lambda c: c.get("position", 0))
    category_map: dict[str, str] = {}

    for ch in channels:
        if ch["type"] != 4:
            continue
        try:
            new_cat = post_channel(s, target_id, {
                "name":                  ch["name"],
                "type":                  4,
                "position":              ch.get("position", 0),
                "permission_overwrites": build_overwrites(ch, role_map, source_id, target_id),
            })
            category_map[ch["id"]] = new_cat["id"]
            print(f"  + category › {ch['name']}")
        except requests.exceptions.HTTPError as e:
            if e.response.status_code in (400, 403):
                print(f"  ~ skipped category › {ch['name']}")
            else:
                raise
        time.sleep(0.4)

    TYPE_LABEL = {0: "text", 2: "voice", 5: "announce", 13: "stage", 15: "forum"}

    for ch in channels:
        if ch["type"] == 4:
            continue

        payload: dict = {
            "name":                  ch["name"],
            "type":                  ch["type"],
            "position":              ch.get("position", 0),
            "permission_overwrites": build_overwrites(ch, role_map, source_id, target_id),
        }

        if ch.get("parent_id") in category_map:
            payload["parent_id"] = category_map[ch["parent_id"]]

        match ch["type"]:
            case 0 | 5:
                if ch.get("topic"):
                    payload["topic"] = ch["topic"]
                if ch.get("nsfw"):
                    payload["nsfw"] = ch["nsfw"]
                if ch.get("rate_limit_per_user"):
                    payload["rate_limit_per_user"] = ch["rate_limit_per_user"]
            case 2 | 13:
                payload["bitrate"]    = min(ch.get("bitrate", 64000), 96000)
                payload["user_limit"] = ch.get("user_limit", 0)

        try:
            post_channel(s, target_id, payload)
            label = TYPE_LABEL.get(ch["type"], f"type{ch['type']}")
            print(f"  + {label:<8} › {ch['name']}")
        except requests.exceptions.HTTPError as e:
            if e.response.status_code in (400, 403):
                print(f"  ~ skipped (type {ch['type']}) › {ch['name']}")
            else:
                raise
        time.sleep(0.4)

    print("\n[+] Clone complete.")

if __name__ == "__main__":
    TOKEN     = "YOUR_DISCORD_USER_TOKEN" # Not from discord.dev
    SOURCE_ID = "SERVER_ID_OF_SERVER_YOU_WANT_TO_COPY"
    TARGET_ID = "SERVER_ID_OF_SERVER_YOU_WANT_TO_PASTE_IT_TOO" # You will need Adminstator Perms + Top Role So all channels and roles are deleted name it * and it makes it easy

    clone_server(SOURCE_ID, TARGET_ID, TOKEN)
