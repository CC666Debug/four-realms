"""Weekly station check for Four Realms (and the sister apps, whose lists it's built from).

1. Tries every stream in stations.json and lists the ones that don't answer or don't send audio.
   Then checks the song info is still updating, for the feeds that say when a song started
   (Halloween Radio and other AzuraCast servers, laut.fm). A renamed channel keeps playing, but its old song info freezes.
2. Searches free public station directories (Radio Browser, laut.fm, TuneIn, SHOUTcast, Icecast/Xiph)
   for each realm's genres, plus the other channels on AzuraCast servers the apps already use,
   and lists stations that aren't in the apps yet.

It only writes a report (Markdown, to stdout). Nothing in the apps changes until the owner OKs it.
Standard library only, so it runs anywhere with Python 3.
"""
import html
import http.client
import json
import re
import socket
import ssl
import time
from collections import defaultdict
import sys
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime
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
NEW_PER_SOURCE = 5   # for each directory after Radio Browser
STALE_HOURS = 3   # no new song for this long means the song info is stuck
HR_API = 'https://radio1.streamserver.link/api/nowplaying'
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
        except http.client.BadStatusLine as e:
            # Old SHOUTcast servers (Radio Caprice etc.) answer "ICY 200 OK" instead of "HTTP/1.0 200 OK".
            # Players take that fine; Python doesn't, so it counts as playing.
            if str(e).startswith('ICY 200'):
                return ''
            reason = 'BadStatusLine: ' + str(e)[:60]
        except (socket.timeout, TimeoutError):
            reason = 'no answer (timed out)'
        except urllib.error.URLError as e:
            reason = 'can’t connect (' + str(e.reason)[:60] + ')'
        except Exception as e:
            reason = type(e).__name__ + ': ' + str(e)[:60]
    return reason


def get_json(url):
    req = urllib.request.Request(url, headers={'User-Agent': UA})
    with urllib.request.urlopen(req, timeout=20, context=SSL_CTX) as r:
        return json.loads(r.read().decode('utf-8'))


def song_age(meta):
    """Hours since the current song started, or a short reason the song info won't load. None if this feed can't tell."""
    try:
        if meta.get('k') == 'azura':
            started = get_json(meta['u'])['now_playing']['played_at']
        elif meta.get('k') == 'laut':
            x = get_json(f'https://api.laut.fm/station/{meta["id"]}/current_song')
            started = datetime.strptime(x['started_at'], '%Y-%m-%d %H:%M:%S %z').timestamp()
        else:
            return None
        return (time.time() - started) / 3600
    except Exception as e:
        return 'song info won’t load (' + type(e).__name__ + ')'


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


def fits(x, tags):
    """Whether a directory hit names one of the realm's genres. Directory searches are loose
    (a "halloween" search on SHOUTcast also turns up 80s and Disney stations)."""
    text = ' '.join((x.get('name') or '', x.get('info') or '', x.get('text') or '')).lower()
    return any(t in text or t.replace(' ', '') in text for t in tags)


def laut_fm(tag):
    """laut.fm search (names, genres and descriptions)."""
    try:
        res = get_json(f'https://api.laut.fm/search/stations?query={urllib.parse.quote(tag)}&limit=40')
    except Exception:
        return []
    found = []
    for cat in res.get('results', []):
        for it in cat.get('items', []):
            st = it.get('station') or {}
            if st.get('stream_url'):
                found.append({'name': st.get('display_name') or st['name'], 'url': st['stream_url'],
                              'info': ', '.join((st.get('genres') or [])[:4]), 'score': 0,
                              'text': ' '.join([st.get('description') or '', st.get('format') or ''] + (st.get('genres') or []))})
    return found


def tunein(tag):
    """TuneIn's free OPML search; each hit's stream is looked up separately."""
    try:
        res = get_json(f'https://opml.radiotime.com/Search.ashx?query={urllib.parse.quote(tag)}&types=station&render=json')
    except Exception:
        return []
    found = []
    for it in res.get('body', []):
        if it.get('item') != 'station' or not it.get('guide_id'):
            continue
        try:
            streams = get_json(f'https://opml.radiotime.com/Tune.ashx?id={it["guide_id"]}&render=json').get('body', [])
        except Exception:
            continue
        if streams and streams[0].get('url'):
            bits = (it.get('formats', ''), f'{it["bitrate"]} kbps' if it.get('bitrate') else '', (it.get('subtext') or '')[:60])
            found.append({'name': it.get('text', ''), 'url': streams[0]['url'],
                          'info': ', '.join(b for b in bits if b), 'score': int(it.get('reliability') or 0)})
        if len(found) >= 15:
            break
    return found


def shoutcast(tag):
    """SHOUTcast directory search (the one its own website uses), most listeners first."""
    try:
        req = urllib.request.Request('https://directory.shoutcast.com/Search/UpdateSearch',
                                     data=urllib.parse.urlencode({'query': tag}).encode(), headers={'User-Agent': UA})
        with urllib.request.urlopen(req, timeout=20) as r:
            res = json.loads(r.read().decode('utf-8'))
    except Exception:
        return []
    found = []
    for st in sorted(res, key=lambda x: x.get('Listeners') or 0, reverse=True)[:15]:
        try:
            req = urllib.request.Request(f'https://yp.shoutcast.com/sbin/tunein-station.pls?id={st["ID"]}', headers={'User-Agent': UA})
            with urllib.request.urlopen(req, timeout=15) as r:
                m = re.search(r'^File1=(\S+)', r.read().decode('utf-8', 'replace'), re.M)
        except Exception:
            continue
        if m:
            bits = (st.get('Genre', ''), f'{st["Bitrate"]} kbps' if st.get('Bitrate') else '', f'{st.get("Listeners", 0)} listening')
            found.append({'name': st.get('Name', ''), 'url': m.group(1),
                          'info': ', '.join(b for b in bits if b), 'score': st.get('Listeners') or 0})
    return found


def xiph(tag):
    """Icecast's public directory (dir.xiph.org). It has no API, so this reads its genre page."""
    try:
        req = urllib.request.Request(f'https://dir.xiph.org/genres/{urllib.parse.quote(tag)}', headers={'User-Agent': UA})
        with urllib.request.urlopen(req, timeout=20) as r:
            page = r.read().decode('utf-8', 'replace')
    except Exception:
        return []
    found = []
    for card in page.split('<div class="card shadow-sm')[1:]:
        name = re.search(r'card-title">(.*?)</h5>', card, re.S)
        play = re.search(r'href="([^"]+)" class="btn btn-sm btn-primary">Play', card)
        if not (name and play):
            continue
        n = re.search(r'(\d+) Listeners', card)
        found.append({'name': html.unescape(name.group(1).strip()), 'url': html.unescape(play.group(1)),
                      'info': ', '.join(html.unescape(t) for t in re.findall(r'badge-secondary">(.*?)</a>', card)[:4]),
                      'score': int(n.group(1)) if n else 0})
    return found


def azuracast_neighbours():
    """Other public channels on the AzuraCast servers the apps already use that name one of the realms' genres."""
    realm_of = defaultdict(lambda: defaultdict(int))
    for st in STATIONS:
        m = st.get('meta') or {}
        if m.get('k') == 'azura':
            realm_of[m['u'].split('/api/')[0]][st['realm']] += 1
    found = defaultdict(list)
    for base in realm_of:
        if base == HR_API.split('/api/')[0]:
            continue   # Halloween Radio's own server has its own section
        try:
            chans = get_json(base + '/api/stations')
        except Exception:
            continue
        for c in chans:
            realm = next((k for k, (_, tags) in REALMS.items() if fits({'name': c.get('name', ''), 'text': c.get('description') or ''}, tags)), None)
            if realm and c.get('is_public', True) and c.get('listen_url'):
                about = urllib.parse.urlsplit(base).hostname + (' · ' + c['description'][:60] if c.get('description') else '')
                found[realm].append({'name': c.get('name', ''), 'url': c['listen_url'], 'info': about, 'score': 0})
    return found


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

    # 1b. Song info that stopped updating (only for stations that still play)
    timed = [s for s in STATIONS if not reasons[s['id']] and (s.get('meta') or {}).get('k') in ('azura', 'laut')]
    with ThreadPoolExecutor(max_workers=8) as pool:
        ages = dict(zip([s['id'] for s in timed], pool.map(lambda s: song_age(s['meta']), timed)))
    stuck = []
    for s in timed:
        a = ages[s['id']]
        if isinstance(a, str):
            stuck.append((s, a))
        elif a is not None and a > STALE_HOURS:
            stuck.append((s, f'no new song for {a:.0f} hours'))
    out.append(f'## Song info stuck ({len(stuck)} of {len(timed)} checked)')
    out.append('')
    if stuck:
        out.append('The music still plays but the song names stopped changing. Often the channel was renamed on its server; for Halloween Radio, see the list below.')
        out.append('')
        out.extend(f'- {s["name"]} ({s.get("group", "")}): {why}' for s, why in stuck)
    else:
        out.append('Every song feed that was checked is updating.')
    out.append('')

    # 1c. Halloween Radio channels on its server that the apps don't use (new or renamed)
    have = {s['meta']['u'].rsplit('/', 1)[-1].removesuffix('.json') for s in STATIONS if (s.get('meta') or {}).get('k') == 'azura'}
    out.append('## Halloween Radio channels not in the app')
    out.append('')
    try:
        extra = [d['station'] for d in get_json(HR_API)
                 if d['station']['shortcode'].startswith('halloween_radio') and d['station']['shortcode'] not in have
                 and d['station']['shortcode'] != 'halloween_radio_premium']
        out.extend(f'- **{st["name"]}** (`{st["shortcode"]}`): ' + (', '.join(m['url'] for m in st.get('mounts', [])) or 'no stream listed')
                   for st in extra)
        if not extra:
            out.append('None.')
    except Exception as e:
        out.append(f'Couldn’t load Halloween Radio’s channel list ({type(e).__name__}).')
    out.append('')

    # 2. New candidates
    have_urls = {norm(s['url']) for s in STATIONS} | {norm(s.get('lite')) for s in STATIONS if s.get('lite')}
    have_names = {s['name'].strip().lower() for s in STATIONS}
    shown = set()
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
            shown.add(norm(st.get('url_resolved') or st.get('url')))
            bits = [st.get('codec') or '', f'{st["bitrate"]} kbps' if st.get('bitrate') else '', st.get('country') or '']
            meta = ', '.join(b for b in bits if b)
            tags_s = ', '.join((st.get('tags') or '').split(',')[:4])
            out.append(f'- **{st["name"].strip()}** ({meta}) · {tags_s}  ')
            out.append(f'  {st.get("url_resolved") or st.get("url")}')
        out.append('')

    # 2b. The other directories, a short list each per realm
    sources = [('laut.fm', laut_fm), ('TuneIn', tunein), ('SHOUTcast', shoutcast), ('Icecast directory', xiph)]
    with ThreadPoolExecutor(max_workers=8) as pool:
        jobs = {(src, tag): pool.submit(fn, tag) for src, fn in sources for _, tags in REALMS.values() for tag in tags}
        neighbours = pool.submit(azuracast_neighbours).result()
    out.append('## More from other directories')
    out.append('')
    out.append('Not in the list above or in the apps. Each stream was tried once; only ones that played are listed.')
    out.append('')
    seen = set(shown)
    for key, (label, tags) in REALMS.items():
        out.append(f'**{label}**')
        groups = [(src, [x for tag in tags for x in jobs[(src, tag)].result()]) for src, _ in sources]
        groups.append(('AzuraCast servers the apps already use', neighbours.get(key, [])))
        any_new = False
        for src, found in groups:
            picks = []
            for x in sorted(found, key=lambda x: x['score'], reverse=True):
                u = norm(x['url'])
                if not u or u in have_urls or u in seen or x['name'].strip().lower() in have_names:
                    continue
                if not fits(x, tags) or 'notcompatible' in u:   # TuneIn's "can't play here" clip
                    continue
                seen.add(u)
                if probe(x['url'], tries=1):
                    continue
                picks.append(x)
                if len(picks) >= NEW_PER_SOURCE:
                    break
            if picks:
                any_new = True
                out.append(f'- *{src}*')
                for x in picks:
                    out.append(f'  - **{x["name"].strip()}**' + (f' · {x["info"]}' if x['info'] else '') + '  ')
                    out.append(f'    {x["url"]}')
        if not any_new:
            out.append('- nothing new found')
        out.append('')

    sys.stdout.write('\n'.join(out) + '\n')


if __name__ == '__main__':
    main()
