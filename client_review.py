"""Revmaxima script review page.

A client opens https://<this app>.streamlit.app/?c=<key>. Per script: why we test it, the hooks and body to edit,
a verdict and notes; Save draft and Send feedback at the bottom. Without a valid key the page shows nothing.

The page reads and writes only the client_review/ folder of the data repository named in the app's secrets
(GITHUB_REPO, GITHUB_TOKEN). It does nothing else.
Local test: set CLIENT_REVIEW_LOCAL_REPO to a folder; the page then reads and writes files there.
"""

import base64
import hmac
import json
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
import streamlit as st
import streamlit.components.v1 as components

st.set_page_config(page_title="Script review", page_icon=":memo:", layout="centered", initial_sidebar_state="collapsed")
st.markdown("<style>#MainMenu, footer, [data-testid='stToolbar'] {visibility: hidden;}</style>", unsafe_allow_html=True)

LOCAL = os.environ.get("CLIENT_REVIEW_LOCAL_REPO")
SAFE = re.compile(r"[A-Za-z0-9_-]{1,60}")
VERDICTS = {"good": "Good to go", "change": "Change it", "drop": "Don't make this video"}
WHY = [("Angle", "angle"), ("Who it speaks to", "audience"), ("Our bet", "bet"), ("Why we think so", "evidence"),
       ("Test type", "test_type"), ("What we will learn", "learn")]
NOTE_MAX, TEXT_MAX, HOOK_MAX = 10000, 6000, 600
# No max_chars on the text boxes: Streamlit silently drops a paste that would go over it. We check the length instead.

# Voice typing: the browser turns speech into text (Chrome, Edge, Safari). No key, no server of ours.
# The button fills the text box right above it; Streamlit saves a text box when it loses focus.
VOICE_HTML = """
<style>
 body {margin: 0; font-family: "Source Sans Pro", "Source Sans 3", sans-serif;}
 button {font: inherit; font-size: 14px; padding: 3px 12px; border-radius: 8px; cursor: pointer;
         border: 1px solid rgba(128,128,128,.45); background: transparent; color: inherit;}
 button.on {border-color: #e5484d; color: #e5484d;}
 button:disabled {cursor: default; opacity: .5;}
 span {font-size: 13px; opacity: .75; margin-left: 8px;}
</style>
<button id="b">🎤 Speak</button><span id="s"></span>
<script>
const P = window.parent, b = document.getElementById("b"), s = document.getElementById("s");
try { document.body.style.color = P.getComputedStyle(P.document.body).color; } catch (e) {}
const SR = P.SpeechRecognition || P.webkitSpeechRecognition;
function target() {
  let last = null;
  for (const t of P.document.querySelectorAll("textarea, input[type='text']"))
    if (t.compareDocumentPosition(window.frameElement) & 4) last = t;   // 4: the button comes after this box
  return last;
}
function add(text) {
  const t = target();
  if (!t || !text.trim()) return;
  const proto = P[t.tagName === "TEXTAREA" ? "HTMLTextAreaElement" : "HTMLInputElement"].prototype;
  t.focus();
  Object.getOwnPropertyDescriptor(proto, "value").set.call(t, t.value + (t.value && !/\\s$/.test(t.value) ? " " : "") + text.trim());
  t.dispatchEvent(new P.Event("input", {bubbles: true}));
  t.blur();
  t.dispatchEvent(new P.FocusEvent("focusout", {bubbles: true}));   // also when the window has no focus
}
let rec = null;
if (!SR) { b.disabled = true; s.textContent = "Voice typing works in Chrome, Edge and Safari."; }
b.onclick = () => {
  if (rec) { rec.stop(); return; }
  rec = new SR();
  rec.continuous = true; rec.interimResults = true; rec.lang = P.navigator.language || "en-US";
  rec.onresult = (e) => {
    let heard = "";
    for (let i = e.resultIndex; i < e.results.length; i++)
      if (e.results[i].isFinal) add(e.results[i][0].transcript); else heard += e.results[i][0].transcript;
    s.textContent = heard || "Listening… click Stop when you are done.";
  };
  rec.onerror = (e) => { s.textContent = e.error === "not-allowed" ? "Allow the microphone for this page, then click Speak again."
                                        : e.error === "no-speech" ? "I heard nothing. Click Speak and try again." : "Voice typing stopped (" + e.error + ")."; };
  rec.onend = () => { rec = null; b.textContent = "🎤 Speak"; b.className = ""; if (s.textContent.startsWith("Listening")) s.textContent = ""; };
  rec.start();
  b.textContent = "⏹ Stop"; b.className = "on"; s.textContent = "Listening… click Stop when you are done.";
};
</script>
"""


# ---------- repo access: only client_review/ ----------

def _checked(path: str) -> str:
    parts = path.split("/")
    if parts[0] != "client_review" or not all(SAFE.fullmatch(p.removesuffix(".json")) for p in parts[1:]):
        raise ValueError(f"path not allowed: {path}")
    return path


if not LOCAL:
    try:
        REPO, TOKEN = st.secrets["GITHUB_REPO"], st.secrets["GITHUB_TOKEN"]
    except Exception:
        st.error("This page is not set up yet.")
        st.stop()
    API = f"https://api.github.com/repos/{REPO}/contents"
    HEADERS = {"Authorization": f"Bearer {TOKEN}", "Accept": "application/vnd.github+json",
               "X-GitHub-Api-Version": "2022-11-28"}


def _fail(resp: requests.Response) -> None:
    st.error(f"Something went wrong on our side ({resp.status_code}). Please try again in a minute, "
             "or tell Revmaxima if it keeps happening.")
    st.stop()


def read(path: str) -> dict | None:
    path = _checked(path)
    if LOCAL:
        p = Path(LOCAL) / path
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None
    resp = requests.get(f"{API}/{path}", headers=dict(HEADERS, Accept="application/vnd.github.raw"), timeout=30)
    if resp.status_code == 404:
        return None
    if not resp.ok:
        _fail(resp)
    return json.loads(resp.content)


def listing(path: str) -> list[dict]:
    """[{name, type}] of a folder."""
    path = _checked(path)
    if LOCAL:
        p = Path(LOCAL) / path
        return [{"name": c.name, "type": "dir" if c.is_dir() else "file"} for c in p.iterdir()] if p.exists() else []
    resp = requests.get(f"{API}/{path}", headers=HEADERS, timeout=20)
    if resp.status_code == 404:
        return []
    if not resp.ok:
        _fail(resp)
    return [{"name": x["name"], "type": x["type"]} for x in resp.json()]


def write(path: str, obj: dict, message: str) -> None:
    path = _checked(path)
    data = json.dumps(obj, indent=1, ensure_ascii=False).encode()
    if LOCAL:
        p = Path(LOCAL) / path
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
        return
    for _ in range(2):   # a job may have committed in between: fetch the newest sha once more
        existing = requests.get(f"{API}/{path}", headers=HEADERS, timeout=20)
        body = {"message": message, "content": base64.b64encode(data).decode()}
        if existing.ok:
            body["sha"] = existing.json()["sha"]
        resp = requests.put(f"{API}/{path}", headers=HEADERS, timeout=20, json=body)
        if resp.ok:
            return
        if resp.status_code not in (409, 422):
            break
    _fail(resp)


@st.cache_data(ttl=20, show_spinner=False)
def batch_files(cid: str, bid: str) -> list[str]:
    return [x["name"] for x in listing(f"client_review/{cid}/{bid}")]


@st.cache_data(ttl=60, show_spinner=False)
def packet(cid: str, bid: str, r: int) -> dict | None:
    return read(f"client_review/{cid}/{bid}/packet_r{r}.json")


# ---------- small helpers ----------

def md(text: str) -> str:
    """Plain text for st.markdown: '$1,499 … $2,099' must not turn into maths."""
    return (text or "").replace("\\", "\\\\").replace("$", "\\$").replace("*", "\\*").replace("_", "\\_")


def when(iso: str | None) -> str:
    if not iso:
        return ""
    return datetime.fromisoformat(iso).astimezone(timezone.utc).strftime("%d %b %Y, %H:%M UTC")


def rounds(files: list[str]) -> list[int]:
    return sorted(int(m.group(1)) for f in files if (m := re.fullmatch(r"packet_r(\d+)\.json", f)))


def voice_button() -> None:
    """🎤 Speak under the text box above it."""
    components.html(VOICE_HTML, height=36)


def length_check(key: str, limit: int, what: str) -> bool:
    """Shows an error under a text box that is too long. True when the length is OK."""
    n = len(st.session_state.get(key) or "")
    if n > limit:
        st.error(f"{what}: {n:,} characters. The limit is {limit:,}. Make the text shorter before you save or send.")
    return n <= limit


# ---------- who is this? ----------

key = st.query_params.get("c", "")
clients = (read("client_review/clients.json") or {}).get("clients", {})
cid = next((c for c, v in clients.items() if v.get("on") and v.get("key") and SAFE.fullmatch(c)
            and hmac.compare_digest(v["key"], key)), None)
if cid is None:
    st.title("Script review")
    st.info("This link is not valid or has been turned off. Ask Revmaxima for a new link.")
    st.stop()
client_name = clients[cid].get("name", cid.title())


# ---------- which batch? ----------

items = []
for d in listing(f"client_review/{cid}"):
    if d["type"] != "dir" or not SAFE.fullmatch(d["name"]):
        continue
    files = batch_files(cid, d["name"])
    shared = [r for r in rounds(files) if (packet(cid, d["name"], r) or {}).get("shared_at")]
    if not shared:
        continue
    r = shared[-1]
    pk = packet(cid, d["name"], r)
    fb = read(f"client_review/{cid}/{d['name']}/feedback_r{r}.json") or {}
    state = "sent" if fb.get("status") == "sent" else "open"
    items.append({"id": d["name"], "r": r, "pk": pk, "fb": fb, "state": state})
# Batches waiting for feedback first (oldest first), then the sent ones (newest first).
items = sorted([x for x in items if x["state"] == "open"], key=lambda x: x["pk"]["shared_at"]) + sorted([x for x in items if x["state"] != "open"],
                                                            key=lambda x: x["pk"]["shared_at"], reverse=True)

st.title(f"{client_name}: scripts for review")
if not items:
    st.info("Nothing to review right now. We will send you a message when new scripts are ready.")
    st.stop()


def label(x: dict) -> str:
    state = "feedback sent, thank you" if x["state"] == "sent" else (
        "revised: see what changed" if x["r"] > 1 else "waiting for your feedback")
    return f"{x['pk']['title']} · {len(x['pk']['scripts'])} scripts · {when(x['pk']['shared_at'])[:11]} · {state}"


pick = st.selectbox("Batch", items, format_func=label) if len(items) > 1 else items[0]
bid, r, pk, fb = pick["id"], pick["r"], pick["pk"], pick["fb"]
prefix = f"{bid}:{r}:"
changes = (read(f"client_review/{cid}/{bid}/changes_r{r - 1}.json") or {}).get("scripts", {}) if r > 1 else {}

st.caption(f"{pk['title']} · {len(pk['scripts'])} scripts" + (f" · round {r}: the revised version after your feedback" if r > 1 else ""))
if pk.get("intro"):
    st.write(pk["intro"])


def why_box(p: dict) -> None:
    with st.container(border=True):
        st.markdown("**Why we test this script**")
        for lab, k in WHY:
            if p["why"].get(k):
                st.markdown(f"**{lab}:** {md(p['why'][k])}")


def changes_box(sid: str) -> None:
    c = changes.get(sid)
    if not c or not (c["changes"] or c["not_changed"] or c.get("other_changes")):
        return
    with st.container(border=True):
        st.markdown("**What we changed after your feedback**")
        for x in c["changes"]:
            said = {"your_note": f"You said: “{md(x['you_said'])}”. We changed: ", "your_edit": "Your edit, kept: "
                    }.get(x.get("kind"), "Our own improvement: ")
            st.markdown(f"- {said}{md(x['we_changed'])}" + (f"  \n  *{md(x['why'])}*" if x.get("why") else ""))
        for x in c["not_changed"]:
            st.markdown(f"- Not changed: “{md(x['you_asked'])}”. Why: {md(x['why'])}")
        if c.get("other_changes"):
            n = c["other_changes"]
            st.markdown(f"- We also made {n} small change{'s' if n > 1 else ''} of our own.")


# ---------- already sent: read only ----------

if fb.get("status") == "sent":
    st.success(f"Thank you{', ' + fb['name'] if fb.get('name') else ''}. We got your feedback on {when(fb.get('sent_at'))}. "
               "Revmaxima now works on it, and you get the next version or the videos.")
    for p in pk["scripts"]:
        f = fb["scripts"].get(p["id"], {})
        st.subheader(f"Script {p['n']} · {p['title']}")
        st.markdown(f"**Your verdict:** {VERDICTS.get(f.get('verdict'), '-')}")
        if (f.get("notes") or "").strip():
            st.markdown(f"**Your notes:** {md(f['notes'])}")
        with st.expander("The script as you sent it, and why we test it"):
            why_box(p)
            for h in f.get("hooks", p["hooks"]):
                if h["text"].strip():
                    st.markdown(f"**Hook {h['k']}:** {md(h['text'])}")
            st.markdown(f"**Body:**\n\n{md(f.get('body') or p['body'])}")
        st.divider()
    st.stop()


# ---------- open: edit, verdict, notes ----------

def k_hook(sid, k): return f"{prefix}{sid}:h{k}"
def k_body(sid): return f"{prefix}{sid}:body"
def k_verdict(sid): return f"{prefix}{sid}:verdict"
def k_notes(sid): return f"{prefix}{sid}:notes"


if not st.session_state.get(f"{prefix}loaded"):   # first open in this browser: the saved draft, else our version
    draft = fb.get("scripts", {}) if fb.get("status") == "draft" else {}
    for p in pk["scripts"]:
        d = draft.get(p["id"], {})
        saved = {h["k"]: h["text"] for h in d.get("hooks", [])}
        for h in p["hooks"]:
            st.session_state[k_hook(p["id"], h["k"])] = saved.get(h["k"], h["text"])
        st.session_state[k_body(p["id"])] = d.get("body", p["body"])
        st.session_state[k_verdict(p["id"])] = d.get("verdict")
        st.session_state[k_notes(p["id"])] = d.get("notes", "")
    st.session_state[f"{prefix}name"] = fb.get("name", "")
    st.session_state[f"{prefix}loaded"] = True


def back_to_ours(p: dict) -> None:
    for h in p["hooks"]:
        st.session_state[k_hook(p["id"], h["k"])] = h["text"]
    st.session_state[k_body(p["id"])] = p["body"]


with st.expander("How to review (about 2 minutes per script)", expanded=not fb):
    st.markdown("1. Read **why we test** each script.\n"
                "2. Edit the hooks or the body if you want something said differently. Each script has up to 3 "
                "**hooks**: different openings (the first ~3 seconds) for the same body. We test all of them. "
                "Empty a hook to drop it.\n"
                "3. Pick **your verdict** and write **your notes**: what to change, and what you like. "
                "You can paste text, or click **🎤 Speak** under the notes box and say it (Chrome, Edge or Safari).\n"
                "4. Click **Send feedback** at the bottom. Click **Save draft** to stop and come back later.")
if fb.get("status") == "draft":
    st.caption(f"Draft saved {when(fb.get('saved_at'))}" + (f" by {fb['name']}" if fb.get("name") else "") + ".")

for p in pk["scripts"]:
    sid = p["id"]
    st.subheader(f"Script {p['n']} · {p['title']}")
    why_box(p)
    changes_box(sid)
    st.markdown("**The script** · edit anything you want to change")
    for h in p["hooks"]:
        st.text_area(f"Hook {h['k']}", key=k_hook(sid, h["k"]), height=68)
        length_check(k_hook(sid, h["k"]), HOOK_MAX, f"Hook {h['k']}")
    st.text_area("Body (the rest of the voiceover)", key=k_body(sid), height=300)
    length_check(k_body(sid), TEXT_MAX, "The body")
    changed = [f"hook {h['k']}" for h in p["hooks"] if st.session_state[k_hook(sid, h["k"])].strip() != h["text"].strip()]
    changed += ["the body"] if st.session_state[k_body(sid)].strip() != p["body"].strip() else []
    if changed:
        c1, c2 = st.columns([3, 2])
        c1.caption("You changed " + ", ".join(changed) + ".")
        c2.button("Back to our version", key=f"{prefix}{sid}:reset", on_click=back_to_ours, args=(p,))
    st.radio("Your verdict", list(VERDICTS), format_func=VERDICTS.get, index=None, horizontal=True, key=k_verdict(sid))
    st.text_area("Your notes", key=k_notes(sid), height=110,
                 placeholder="What should we change or improve? What do you like?")
    voice_button()
    length_check(k_notes(sid), NOTE_MAX, "Your notes")
    st.divider()


def too_long() -> list[str]:
    """Scripts with a text box over its limit."""
    out = []
    for p in pk["scripts"]:
        sid = p["id"]
        boxes = [(k_hook(sid, h["k"]), HOOK_MAX) for h in p["hooks"]] + [(k_body(sid), TEXT_MAX), (k_notes(sid), NOTE_MAX)]
        if any(len(st.session_state.get(k) or "") > limit for k, limit in boxes):
            out.append(str(p["n"]))
    return out


def collect(status: str) -> dict:
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    out = {"round": r, "status": status, "name": st.session_state.get(f"{prefix}name", "").strip()[:80],
           "saved_at": now, "sent_at": now if status == "sent" else None, "scripts": {}}
    for p in pk["scripts"]:
        sid = p["id"]
        out["scripts"][sid] = {"verdict": st.session_state.get(k_verdict(sid)),
                               "hooks": [{"k": h["k"], "text": st.session_state[k_hook(sid, h["k"])][:HOOK_MAX]} for h in p["hooks"]],
                               "body": st.session_state[k_body(sid)][:TEXT_MAX],
                               "notes": (st.session_state.get(k_notes(sid)) or "")[:NOTE_MAX]}
    return out


def save(status: str) -> bool:
    if time.time() - st.session_state.get("last_save", 0) < 5:
        st.warning("Saved a moment ago. Wait 5 seconds, then try again.")
        return False
    if long := too_long():
        st.error(f"A text in script {', '.join(long)} is too long. Make it shorter (see the red message under it), then try again.")
        return False
    current = read(f"client_review/{cid}/{bid}/feedback_r{r}.json") or {}
    if current.get("status") == "sent":
        st.error(f"This feedback was already sent on {when(current.get('sent_at'))}"
                 + (f" by {current['name']}" if current.get("name") else "") + ". Reload the page to see it.")
        return False
    write(f"client_review/{cid}/{bid}/feedback_r{r}.json", collect(status),
          f"Client {cid}: {'sent' if status == 'sent' else 'saved a draft of'} feedback on {bid} round {r}")
    st.session_state["last_save"] = time.time()
    return True


@st.dialog("Send your feedback?")
def confirm_send() -> None:
    st.write("Revmaxima gets it at once. You can't change it after sending.")
    if st.button("Yes, send it", type="primary"):
        if save("sent"):
            st.session_state.pop(f"{prefix}loaded", None)
            st.rerun()


st.text_input("Your name", key=f"{prefix}name", max_chars=80)
c1, c2 = st.columns(2)
if c1.button("Save draft", use_container_width=True):
    if save("draft"):
        st.success("Draft saved. Open the same link later, on any device, to go on.")
if c2.button("Send feedback", type="primary", use_container_width=True):
    missing = [str(p["n"]) for p in pk["scripts"] if not st.session_state.get(k_verdict(p["id"]))]
    if missing:
        st.error(f"Pick a verdict for script {', '.join(missing)} first.")
    elif long := too_long():
        st.error(f"A text in script {', '.join(long)} is too long. Make it shorter (see the red message under it), then send.")
    else:
        confirm_send()
