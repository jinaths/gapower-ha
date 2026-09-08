"""One-shot diagnostic for the gapower login chain. Prints NO credentials.

⚠️ KNOWN UNRELIABLE - read before trusting any output from this (2026-09-08).
Two runs on different days produced BYTE-IDENTICAL output, down to the step-1 login
token and the Incapsula session cookie values. Live session tokens cannot repeat
across days, so at least one of those runs did not reflect the real server state.
Best explanation: this uses plain `urllib`, whose TLS handshake and header ordering
fingerprint differently to Imperva than the integration's `aiohttp` client, so it
can land in a different WAF bucket and get served a cached or synthetic response.

Until that is fixed, prefer either:
  * `ha_get_integration` -> the config entry's `reason` field (the real code path), or
  * a browser HAR capture (what actually identified the 2026-09 ForgeRock migration).
Do not conclude anything about the portal's state from this script alone.

Dumps every hop of the four-step token relay: status, Location, Set-Cookie names
(values redacted, lengths only), response header names, cookie-jar contents, and a
sanitised body snippet. Reads the account from HA's own config entry so the
password never leaves the box.

Stdlib only - runs in the SSH add-on's Alpine container with just `apk add python3`.

HOW TO RUN (no shell typing, same pattern as the HACS bootstrap in CLAUDE.md):
  1. gzip + base64 this file into one line:
       $in=[IO.File]::ReadAllBytes('tools/login_diag.py')
       $ms=New-Object IO.MemoryStream
       $g=New-Object IO.Compression.GZipStream($ms,[IO.Compression.CompressionLevel]::SmallestSize)
       $g.Write($in,0,$in.Length); $g.Close()
       [Convert]::ToBase64String($ms.ToArray())
  2. ha_manage_app(slug="a0d7b954_ssh", options={"packages":["python3"],
       "init_commands":["command -v python3 >/dev/null 2>&1 || apk add --no-cache python3 >/dev/null 2>&1; "
                        "echo '<B64>' | base64 -d | gunzip > /tmp/gpdiag.py && "
                        "timeout 150 python3 /tmp/gpdiag.py 2>&1; echo DIAG-END=$?"]})
  3. ha_manage_app(slug="a0d7b954_ssh", action="restart")
  4. ha_get_logs(source="supervisor", slug="a0d7b954_ssh", order="newest")
  5. Clear it back to {"packages":[],"init_commands":[]} and restart - one-shot only.

The `timeout` matters: init_commands run synchronously in the s6 init chain, so a
hanging request would block ttyd from starting and 502 the Web UI.
"""
import http.cookiejar, json, os, re, urllib.error, urllib.parse, urllib.request

WEBAUTH = "https://webauth.southernco.com"
CS2 = "https://customerservice2.southerncompany.com"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
H = {"User-Agent": UA,
     "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
     "Accept-Language": "en-US,en;q=0.9",
     "Sec-Ch-Ua": '"Chromium";v="126", "Not)A;Brand";v="99", "Google Chrome";v="126"',
     "Sec-Ch-Ua-Mobile": "?0", "Sec-Ch-Ua-Platform": '"Windows"',
     "Upgrade-Insecure-Requests": "1"}

SECRETS = []


def sane(s):
    if s is None:
        return None
    s = str(s)
    for sec in SECRETS:
        if sec:
            s = s.replace(sec, "<SECRET>")
    return re.sub(r"[A-Za-z0-9_\-.%]{40,}", lambda m: "<%dchars>" % len(m.group(0)), s)


def creds():
    for p in ("/config/.storage/core.config_entries",
              "/homeassistant/.storage/core.config_entries"):
        if os.path.exists(p):
            with open(p) as fh:
                d = json.load(fh)
            for e in d["data"]["entries"]:
                if e.get("domain") == "gapower":
                    print("DIAG: found gapower entry in %s" % p)
                    return e["data"]["username"], e["data"]["password"]
    raise SystemExit("DIAG: FATAL gapower entry not found")


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


cj = http.cookiejar.CookieJar()
op = urllib.request.build_opener(NoRedirect, urllib.request.HTTPCookieProcessor(cj))


def call(method, url, data=None, hdrs=None, as_json=False):
    h = dict(H)
    h.update(hdrs or {})
    body = None
    if data is not None:
        if as_json:
            body = json.dumps(data).encode()
            h["Content-Type"] = "application/json"
        else:
            body = urllib.parse.urlencode(data).encode()
            h["Content-Type"] = "application/x-www-form-urlencoded"
    req = urllib.request.Request(url, data=body, headers=h, method=method)
    try:
        r = op.open(req, timeout=45)
        return r.status, r.read().decode("utf-8", "replace"), r.headers
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace"), e.headers


def dump(tag, status, hdrs, body, blen=400):
    print("DIAG %s: status=%s" % (tag, status))
    print("DIAG %s: Location=%s" % (tag, sane(hdrs.get("Location"))))
    print("DIAG %s: Content-Type=%s" % (tag, hdrs.get("Content-Type")))
    sc = hdrs.get_all("Set-Cookie") or []
    print("DIAG %s: %d Set-Cookie header(s)" % (tag, len(sc)))
    for raw in sc:
        name, _, rest = raw.partition("=")
        val, semi, attrs = rest.partition(";")
        print("DIAG %s:   cookie=%r vallen=%d semicolon=%s attrs=%r"
              % (tag, name.strip(), len(val), bool(semi), attrs.strip()[:120]))
    print("DIAG %s: resp-hdrs=%s" % (tag, sorted({k for k, _ in hdrs.items()})))
    print("DIAG %s: jar=%s" % (tag, sorted({"%s@%s" % (c.name, c.domain) for c in cj})))
    print("DIAG %s: body[:%d]=%s" % (tag, blen, sane(body[:blen])))
    print("DIAG %s: ---" % tag)


user, pw = creds()
SECRETS.extend([user, pw, urllib.parse.quote(user), urllib.parse.quote(pw)])

# ---- step 1: verification token -------------------------------------------
st, body, hd = call("POST", WEBAUTH + "/webservices/api/WebUser/Login",
                    data={"username": user, "password": pw}, as_json=True,
                    hdrs={"Referer": WEBAUTH + "/SPA/OCC/login",
                          "Accept": "application/json, text/plain, */*"})
dump("STEP1", st, hd, body, 250)
try:
    tok = (json.loads(body).get("data") or {}).get("token")
except ValueError:
    tok = None
print("DIAG STEP1: token_found=%s len=%s" % (bool(tok), len(tok or "")))
if not tok:
    raise SystemExit("DIAG: FATAL no token at step 1")

# ---- step 2: ScWebToken ----------------------------------------------------
st, body, hd = call("POST", WEBAUTH + "/SPA/Navigation",
                    data={"Token": tok, "ReturnUrl": "none"})
dump("STEP2", st, hd, body, 400)
print("DIAG STEP2: hidden-inputs=%s" % re.findall(r'name="([^"]+)"', body)[:20])
print("DIAG STEP2: form-actions=%s" % [sane(a) for a in re.findall(r'action="([^"]*)"', body)[:5]])
m = re.search(r'name="ScWebToken"\s+value="(\S+\.\S+\.\S+)"', body, re.I)
print("DIAG STEP2: ScWebToken_found=%s" % bool(m))
if not m:
    m2 = re.search(r'name="ScWebToken"\s+value="([^"]*)"', body, re.I)
    print("DIAG STEP2: loose_match=%s len=%s"
          % (bool(m2), len(m2.group(1)) if m2 else 0))
    raise SystemExit("DIAG: FATAL no ScWebToken at step 2")

# ---- step 3: LoginComplete -> SouthernJwtCookie -----------------------------
st, body, hd = call("POST", CS2 + "/Account/LoginComplete?ReturnUrl=/Billing/Home",
                    data={"ScWebToken": m.group(1)})
dump("STEP3", st, hd, body, 600)

# ---- step 3b: follow the redirect one hop, cookie may be set later ----------
loc = hd.get("Location")
if loc:
    nxt = urllib.parse.urljoin(CS2 + "/Account/LoginComplete", loc)
    print("DIAG STEP3B: following -> %s" % sane(nxt))
    st, body, hd = call("GET", nxt)
    dump("STEP3B", st, hd, body, 600)
    loc2 = hd.get("Location")
    if loc2:
        nxt2 = urllib.parse.urljoin(nxt, loc2)
        print("DIAG STEP3C: following -> %s" % sane(nxt2))
        st, body, hd = call("GET", nxt2)
        dump("STEP3C", st, hd, body, 600)

sjc = next((c.value for c in cj if c.name == "SouthernJwtCookie"), None)
print("DIAG: SouthernJwtCookie_in_jar=%s len=%s" % (bool(sjc), len(sjc or "")))

# ---- step 4: only if we actually have the cookie ----------------------------
if sjc:
    st, body, hd = call("GET", CS2 + "/Account/LoginValidated/JwtToken",
                        hdrs={"Cookie": "SouthernJwtCookie=" + sjc})
    dump("STEP4", st, hd, body, 300)
    jwt = next((c.value for c in cj if c.name == "ScJwtToken"), None)
    print("DIAG: ScJwtToken_in_jar=%s len=%s" % (bool(jwt), len(jwt or "")))
print("DIAG: DONE")
