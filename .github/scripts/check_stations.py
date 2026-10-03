"""Weekly station check for Four Realms (and the sister apps, whose lists it's built from).

1. Tries every stream in stations.json and lists the ones that don't answer or don't send audio.
2. Searches Radio Browser (a free public station directory) for each realm's genres and lists
   popular stations that aren't in the apps yet.

It only writes a report (Markdown, to stdout). Nothing in the apps changes until the owner OKs it.
Standard library only, so it runs anywhere with Python 3.
"""
import json
import socket
import ssl
import time
from collections import defaultdict
import sys
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from pathlib import Path

UA = 'FourRealmsStationCheck/1.0 (+https://cc666debug.github.io/four-realms/)'
ROOT = Path(__file__).resolve().parents[2]
STATIONS = json.loads((ROOT / 'stations.json').read_text(encoding='utf-8'))

REALMS = {
    'halloween': ('🎃 Halloween', ['halloween', 'horror', 'spooky']),
    'synth': ('🌆 Synth', ['synthwave', 'retrowave', 'outrun', 'darksynth', 'vaporwave', 'chiptune']),
    'pagan': ('🌙 Pagan', ['pagan', 'neofolk', 'folk metal', 'viking', 'celtic', 'medieval', 'dark folk']),
    'kvlt': ('🐐 Kvlt', ['black metal', 'death metal', 'doom metal', 'dark ambient']),
}
NEW_PER_REALM = 10
# Only "does audio come out" matters here, like a player would see it, so certificate quirks don't count as dead.
SSL_CTX = ssl.create_default_context()
SSL_CTX.check_hostname = False
SSL_CTX.verify_mode = ssl.CERT_NONE


def probe(url, tries=2):
    """Returns '' if the stream answers with audio, else a short reason."""
    reason = ''
    for attempt in range(tries):
        if attempt:
            time.sleep(4)
        try:
            req = urllib.request.Request(url, headers={'User-Agent': UA, 'Icy-MetaData': '0'})
            with urllib.request.urlopen(req, timeout=15, context=SSL_CTX) as r:
                ctype = (r.headers.get('Content-Type') or '').lower()
                data = r.read(4096)
                if not data:
                    reason = 'answers but sends nothing'
                elif 'text/html' in ctype:
                    reason = 'sends a web page, not audio'
                else:
                    return ''
        except urllib.error.HTTPError as e:
            reason = f'HTTP {e.code}'
        except (socket.timeout, TimeoutError):
            reason = 'no answer (timed out)'
        except urllib.error.URLError as e:
            reason = 'can’t connect (' + str(e.reason)[:60] + ')'
        except Exception as e:
            reason = type(e).__name__ + ': ' + str(e)[:60]
    return reason


def norm(url):
    """A stream's address without the scheme, a trailing slash or a query, for spotting duplicates."""
    p = urllib.parse.urlsplit((url or '').strip().lower())
    return (p.hostname or '') + (':' + str(p.port) if p.port else '') + p.path.rstrip('/')


def radio_browser(tag):
    q = urllib.parse.quote(tag)
    for host in ('de1', 'nl1', 'at1', 'fi1'):
        url = f'https://{host}.api.radio-browser.info/json/stations/bytag/{q}?hidebroken=true&order=clickcount&reverse=true&limit=60'
        try:
            req = urllib.request.Request(url, headers={'User-Agent': UA})
            with urllib.request.urlopen(req, timeout=20) as r:
                return json.loads(r.read().decode('utf-8'))
        except Exception:
            continue
    return []


def main():
    out = [f'# Station check · {date.today():%b %d, %Y}', '']

    # 1. Dead stations
    # One station at a time per server (many channels share one, and a burst of connections gets throttled),
    # different servers in parallel.
    by_host = defaultdict(list)
    for s in STATIONS:
        by_host[urllib.parse.urlsplit(s['url']).hostname or ''].append(s)
    reasons = {}
    def run_host(group):
        for s in group:
            reasons[s['id']] = probe(s['url'])
    with ThreadPoolExecutor(max_workers=16) as pool:
        list(pool.map(run_host, by_host.values()))
    dead = [(s, reasons[s['id']]) for s in STATIONS if reasons[s['id']]]
    out.append(f'## Not answering ({len(dead)} of {len(STATIONS)})')
    out.append('')
    if dead:
        out.append('Checked twice, a few seconds apart. Some come back on their own, so check one is still out next week before removing it.')
        out.append('')
        for key, (label, _) in REALMS.items():
            rows = [(s, why) for s, why in dead if s['realm'] == key]
            if not rows:
                continue
            out.append(f'**{label}**')
            out.extend(f'- {s["name"]} ({s.get("group", "")}): {why}' for s, why in rows)
            out.append('')
    else:
        out.append('Every station answered. 🎉')
        out.append('')

    # 2. New candidates
    have_urls = {norm(s['url']) for s in STATIONS} | {norm(s.get('lite')) for s in STATIONS if s.get('lite')}
    have_names = {s['name'].strip().lower() for s in STATIONS}
    out.append('## Stations you might add')
    out.append('')
    out.append('Popular in Radio Browser for each realm’s genres, and not in the apps yet. Say which ones you want.')
    out.append('')
    for key, (label, tags) in REALMS.items():
        seen, picks = set(), []
        for tag in tags:
            for st in radio_browser(tag):
                u = norm(st.get('url_resolved') or st.get('url'))
                name = (st.get('name') or '').strip()
                if not u or u in have_urls or u in seen or name.lower() in have_names:
                    continue
                if (st.get('lastcheckok') or 0) != 1:
                    continue
                if any(t in (st.get('tags') or '').lower() for t in ('news', 'talk', 'propaganda', 'sports')):
                    continue   # music stations only
                seen.add(u)
                picks.append(st)
        picks.sort(key=lambda st: st.get('clickcount') or 0, reverse=True)
        out.append(f'**{label}**')
        if not picks:
            out.append('- nothing new found')
        for st in picks[:NEW_PER_REALM]:
            bits = [st.get('codec') or '', f'{st["bitrate"]} kbps' if st.get('bitrate') else '', st.get('country') or '']
            meta = ', '.join(b for b in bits if b)
            tags_s = ', '.join((st.get('tags') or '').split(',')[:4])
            out.append(f'- **{st["name"].strip()}** ({meta}) · {tags_s}  ')
            out.append(f'  {st.get("url_resolved") or st.get("url")}')
        out.append('')

    sys.stdout.write('\n'.join(out) + '\n')


if __name__ == '__main__':
    main()
