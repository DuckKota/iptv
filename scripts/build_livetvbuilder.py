#!/usr/bin/env python3
"""Build one nationwide combined.m3u + combined.xml from livetvbuilder.

Run by .github/workflows/livetvbuilder.yml, which publishes the outputs to the
`state` branch. The fork consumes them as a `sources.yml` m3u entry and an
`epg-sources.yml` guide entry, so this script only fetches and merges; trust,
health scoring, channel matching and EPG linking all happen in the fork.

Guide ids are emitted with the '@feed' suffix stripped (see base_id) so they
match the fork's channel ids, which carry no suffix.

Uses the /api/download/bundle endpoint, which returns a zip holding both the
provider's .m3u and -guide.xml for a ZIP. That is one request per
(zip, provider) instead of two, and the zip is already deflate-compressed
(~7x), so no Accept-Encoding juggling is needed.

Per bundle, EPG channels are exactly the bundle's m3u IDs, so EPG is pruned
against that same bundle in a single pass. Streams, channels and programmes
are deduped globally (by URL, id, and (channel, start) respectively);
programmes stream to a temp file instead of living in RAM.

Defaults (no flags): 500 top US cities (embedded) x us-live-community,big-150.
That is ~99.3% of all channels in ~22 minutes. The server generates one lineup
per (zip, provider) at ~2.5s serially, so fewer ZIPs/providers = faster.

Robustness (safe for cron / every-8-hours automation):
  * Never crashes: unexpected errors are logged and exit 0 by default.
  * Retries: transient network/5xx retried per request; failed ZIPs retried in
    rounds; a dropped internet recovers on the next round.
  * Validates every bundle (zip, m3u entries, xml) and every output file.
  * Atomic writes: outputs written to .tmp then renamed, so a crash never
    leaves a half-written playlist.
  * Never overwrites good output with bad: keeps the previous combined.m3u/xml
    if this run produced no/too little data (--min-success, --min-ratio).
  * Logs to .build_us_playlists.log and holds a lock file so overlapping runs
    don't collide. Pass --strict to exit non-zero on failure (for alerting).

Usage:
    python3 build_us_playlists.py                 # the default fast run
    python3 build_us_playlists.py --sample none   # all 42,366 ZIPs
    python3 build_us_playlists.py --sample prefix # 912 ZIPs
    python3 build_us_playlists.py --providers us-live-community,big-150,dish
    python3 build_us_playlists.py --zips-file my_zips.txt
"""
import argparse
import csv
import http.client
import io
import logging
import os
import random
import re
import struct
import sys
import tempfile
import threading
import time
import traceback
import urllib.parse
import xml.etree.ElementTree as ET
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed

try:
    import fcntl
except ImportError:  # non-POSIX
    fcntl = None

HOST = "livetvbuilder.replit.app"
PROVIDERS = ["directv-stream", "dish", "youtube-tv", "big-150", "us-live-community"]
DEFAULT_PROVIDERS = ["us-live-community", "big-150"]
DEFAULT_ZIPS = [
    "01013", "01101", "01601", "01701", "01801", "01810", "01850", "01901", "02101", "02138", "02301", "02720",
    "02740", "02901", "03060", "03101", "04101", "04330", "05601", "06101", "06107", "06430", "06501", "06601",
    "06701", "06810", "06850", "06901", "07011", "07097", "07101", "07201", "07501", "08101", "08540", "08601",
    "08753", "08817", "08901", "10001", "10301", "10451", "10550", "10601", "10701", "11020", "11040", "11050",
    "11201", "11351", "11405", "11530", "11590", "11801", "12179", "12201", "12301", "13201", "13501", "13901",
    "14201", "14301", "14601", "14901", "15130", "15201", "15901", "16101", "16365", "16501", "17001", "17101",
    "17401", "17601", "18015", "18040", "18101", "18501", "18701", "19019", "19054", "19401", "19481", "19601",
    "19702", "19801", "19901", "20001", "20101", "20108", "20146", "20163", "20170", "20190", "20707", "20715",
    "20731", "20772", "20781", "20813", "20847", "20877", "20901", "21201", "21401", "21501", "21660", "21701",
    "21740", "22030", "22040", "22081", "22101", "22150", "22180", "22191", "22201", "22301", "22401", "22601",
    "22901", "23218", "23320", "23432", "23450", "23501", "23601", "23630", "23701", "24001", "24501", "25301",
    "25401", "25701", "26101", "26501", "27101", "27260", "27401", "27511", "27514", "27601", "27701", "28201",
    "28301", "28401", "28801", "29201", "29301", "29401", "29405", "29501", "29572", "29601", "29621", "29801",
    "29901", "30003", "30006", "30030", "30042", "30301", "30601", "30901", "31201", "31401", "31601", "31701",
    "31901", "32080", "32099", "32114", "32301", "32401", "32501", "32601", "32801", "32901", "32905", "32960",
    "33002", "33019", "33030", "33060", "33101", "33109", "33301", "33401", "33424", "33427", "33444", "33454",
    "33539", "33601", "33701", "33755", "33770", "33801", "33880", "33900", "33904", "33948", "34101", "34201",
    "34230", "34470", "34601", "34606", "34741", "34945", "34952", "35201", "35401", "35801", "35901", "36101",
    "36201", "36601", "37127", "37201", "37401", "37501", "37601", "37660", "37901", "38301", "39201", "39301",
    "39401", "39501", "39530", "39701", "40201", "40502", "40601", "41901", "43085", "43601", "44101", "44301",
    "44481", "44501", "44701", "44901", "45011", "45201", "45400", "45501", "45801", "46011", "46201", "46401",
    "46601", "46801", "47130", "47302", "47401", "47701", "47801", "47901", "47933", "48007", "48033", "48088",
    "48103", "48120", "48201", "48331", "48501", "48601", "48640", "48901", "49001", "49440", "49501", "50301",
    "50701", "51101", "52240", "52401", "52801", "53201", "53401", "53701", "54301", "54911", "55101", "55348",
    "55357", "55362", "55394", "55401", "55801", "55901", "56301", "57101", "58102", "58201", "58501", "59101",
    "59601", "59715", "59801", "60038", "60116", "60159", "60201", "60403", "60502", "60540", "60601", "61101",
    "61601", "61701", "61820", "62201", "62521", "62701", "63101", "64050", "64063", "64101", "64501", "65101",
    "65201", "65801", "66101", "66201", "66601", "67201", "68101", "68501", "70001", "70112", "70500", "70601",
    "70801", "71101", "71201", "71301", "71901", "72114", "72201", "72901", "73019", "73101", "73301", "74101",
    "75014", "75023", "75040", "75149", "75201", "75501", "75601", "75701", "76001", "76101", "76201", "76301",
    "76501", "76540", "76701", "76901", "77001", "77301", "77320", "77325", "77373", "77449", "77501", "77550",
    "77701", "77801", "77840", "78040", "78201", "78401", "79101", "79401", "79601", "79701", "79760", "79901",
    "80001", "80010", "80110", "80120", "80201", "80301", "80521", "80631", "80901", "81001", "81501", "82001",
    "83201", "83401", "83701", "84070", "84101", "84201", "84601", "85001", "85123", "85190", "85201", "85224",
    "85250", "85280", "85301", "85345", "85701", "86301", "87101", "87500", "88001", "89002", "89030", "89044",
    "89431", "89501", "89701", "90001", "90301", "90401", "90501", "90601", "90801", "91050", "91201", "91324",
    "91388", "91501", "91601", "91766", "91801", "91909", "92025", "92049", "92101", "92401", "92501", "92551",
    "92602", "92605", "92658", "92701", "92801", "92831", "92840", "92856", "92877", "93001", "93030", "93062",
    "93101", "93277", "93301", "93401", "93534", "93550", "93650", "93901", "94035", "94059", "94085", "94101",
    "94203", "94301", "94401", "94518", "94540", "94601", "94701", "94801", "94952", "95050", "95060", "95101",
    "95201", "95350", "95401", "95926", "96150", "96801", "97005", "97086", "97301", "97401", "98004", "98101",
    "98201", "98401", "98501", "98660", "98901", "99201", "99501", "99701",
]
ZIP_CSV = "https://raw.githubusercontent.com/midwire/free_zipcode_data/refs/heads/develop/all_us_zipcodes.csv"
UA = "build_us_playlists/1.0"
TVG_ID = re.compile(r'tvg-id="([^"]*)"')
EXTINF = re.compile(r"^#EXTINF", re.M)

log = logging.getLogger("build_us_playlists")


def base_id(tvg_id):
    """Strip the livetvbuilder '@feed' suffix (e.g. 'BBCNews.us@SD' -> 'BBCNews.us').

    The fork's stream matcher already strips '@feed' when resolving channels, but
    its EPG matcher does not, so the emitted guide must carry base ids to link.
    """
    return tvg_id.split("@", 1)[0]


def setup_logging(log_path):
    log.setLevel(logging.DEBUG)
    log.handlers.clear()
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s",
                            datefmt="%Y-%m-%d %H:%M:%S")
    stream = logging.StreamHandler(sys.stderr)
    stream.setFormatter(fmt)
    log.addHandler(stream)
    try:
        fh = logging.FileHandler(log_path, encoding="utf-8")
        fh.setFormatter(fmt)
        log.addHandler(fh)
    except OSError as exc:
        log.warning("could not open log file %s: %s", log_path, exc)

_thread = threading.local()


class _Permanent(Exception):
    """A 4xx response: retrying will not help."""


def read_exact(f, n):
    """Read exactly n bytes from f."""
    buf = f.read(n)
    while len(buf) < n:
        more = f.read(n - len(buf))
        if not more:
            raise EOFError("truncated spool")
        buf += more
    return buf


def _conn():
    conn = getattr(_thread, "conn", None)
    if conn is None:
        conn = http.client.HTTPSConnection(HOST, timeout=180)
        _thread.conn = conn
    return conn


def fetch(path, retries=6):
    """GET a path over a reused keep-alive connection (no TLS per request)."""
    last = None
    for attempt in range(retries):
        try:
            conn = _conn()
            conn.request("GET", path, headers={"User-Agent": UA, "Connection": "keep-alive"})
            resp = conn.getresponse()
            data = resp.read()
            status = resp.status
            if status != 200:
                if 400 <= status < 500:
                    raise _Permanent(f"HTTP {status}")
                raise RuntimeError(f"HTTP {status}")
            if not data:
                raise RuntimeError("empty response body")
            return data
        except _Permanent:
            raise
        except Exception as exc:
            last = exc
            try:
                _thread.conn.close()
            except Exception:
                pass
            _thread.conn = None
            if attempt == retries - 1:
                break
            time.sleep(min(8.0, 0.5 * 2 ** attempt) + random.random())
    raise last


def bundle_path(zipcode, provider):
    return "/api/download/bundle?" + urllib.parse.urlencode(
        {"zip": zipcode, "providerId": provider, "country": "US"})


SAMPLE_KEYS = {
    "county": lambda r: (r.get("state", ""), r.get("county", "")),
    "city": lambda r: (r.get("state", ""), r.get("city", "")),
    "state": lambda r: r.get("state", ""),
    "prefix": lambda r: r["code"].strip().zfill(5)[:3],
}


def get_zips(csv_path, explicit, zips_file, sample):
    if zips_file:
        codes = []
        with open(zips_file) as f:
            for ln in f:
                token = ln.split("#", 1)[0].strip()
                if token:
                    codes.append(token.split()[0].zfill(5))
        return sorted(set(codes))
    if explicit:
        return sorted(set(explicit))
    if sample == "embedded":
        return list(DEFAULT_ZIPS)
    if csv_path:
        raw = open(csv_path, "rb").read()
    else:
        import urllib.request
        req = urllib.request.Request(ZIP_CSV, headers={"User-Agent": UA})
        raw = urllib.request.urlopen(req, timeout=180).read()
    reader = csv.DictReader(io.StringIO(raw.decode("utf-8-sig")))
    rows = [row for row in reader if row.get("code", "").strip()]
    if sample != "none":
        key = SAMPLE_KEYS[sample]
        seen = set()
        picked = []
        for row in rows:
            k = key(row)
            if k in seen:
                continue
            seen.add(k)
            picked.append(row)
        rows = picked
    codes = {row["code"].strip().zfill(5) for row in rows}
    return sorted(codes)


def run_pool(zips, work, workers, label):
    total = len(zips)
    step = max(1, total // 50)
    started = time.time()
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(work, z) for z in zips]
        for done, fut in enumerate(as_completed(futures), 1):
            fut.result()
            if done % step == 0 or done == total:
                elapsed = time.time() - started
                rate = done / max(elapsed, 1e-9)
                eta = (total - done) / rate if rate else 0
                pct = done / total * 100
                print(f"  {label}: {done}/{total} ({pct:5.1f}%) "
                      f"elapsed {elapsed/60:.1f}m  eta {eta/60:.1f}m  {rate:.2f} zip/s",
                      file=sys.stderr, flush=True)


def parse_m3u(text):
    entries = []
    extinf = None
    for line in text.splitlines():
        line = line.rstrip("\r")
        if line.startswith("#EXTINF"):
            extinf = line
        elif line.startswith("#"):
            continue
        elif line.strip() and extinf is not None:
            entries.append((extinf, line.strip()))
            extinf = None
    return entries


def validate_bundle(blob):
    """Return (m3u_text, xml_bytes); raise ValueError on empty/corrupt bundle."""
    if not blob:
        raise ValueError("empty response")
    try:
        zf = zipfile.ZipFile(io.BytesIO(blob))
        names = zf.namelist()
        m3u_name = next((n for n in names if n.endswith(".m3u")), None)
        xml_name = next((n for n in names if n.endswith(".xml")), None)
        if not m3u_name or not xml_name:
            raise ValueError(f"missing members: {names}")
        text = zf.read(m3u_name).decode("utf-8", "replace")
        xml_bytes = zf.read(xml_name)
    except zipfile.BadZipFile as exc:
        raise ValueError(f"corrupt zip: {exc}")
    if not EXTINF.search(text):
        raise ValueError("m3u has no entries")
    if not xml_bytes.strip():
        raise ValueError("empty xml")
    return text, xml_bytes


def parse_bundle(text, xml_bytes):
    """Parse one bundle into (m3u entries, channel ids, xml root)."""
    entries = parse_m3u(text)
    ids = set()
    for extinf, _ in entries:
        match = TVG_ID.search(extinf)
        if match and match.group(1):
            ids.add(base_id(match.group(1)))
    tv = ET.fromstring(xml_bytes)  # raises ET.ParseError on corrupt xml
    return entries, ids, tv


def _m3u_ok(path):
    try:
        with open(path, encoding="utf-8") as f:
            if f.readline().strip() != "#EXTM3U":
                return False
            return f.readline().startswith("#EXTINF")
    except OSError:
        return False


def _xml_ok(path):
    try:
        with open(path, "rb") as f:
            head = f.read(512)
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - 512))
            tail = f.read()
    except OSError:
        return False
    return (size > 1000 and head.lstrip().startswith(b"<?xml")
            and b"<tv" in head and b"</tv>" in tail)


def _count_extinf(path):
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return sum(1 for line in f if line.startswith("#EXTINF"))
    except OSError:
        return 0


def build(zips, workers, m3u_path, xml_path, providers, rounds=3,
          min_success=0.5, min_ratio=0.5):
    lock = threading.Lock()
    seen_stream = set()
    ordered = []
    channel_ids = set()
    channels = {}
    # ponytail: programme dedupe set lives in RAM; full US run may need several
    # GB. Swap for an on-disk/sqlite key set if it OOMs.
    seen_prog = set()
    ok_zips = set()
    fail_zips = set()
    spool = tempfile.NamedTemporaryFile("w+b", delete=False, suffix=".xml")

    def work(zipcode):
        m3u_buf = []
        chan_buf = []
        prog_buf = []
        got_any = False
        for provider in providers:
            try:
                blob = fetch(bundle_path(zipcode, provider))
                text, xml_bytes = validate_bundle(blob)
                entries, ids, tv = parse_bundle(text, xml_bytes)
            except _Permanent as exc:
                log.warning("bundle %s/%s: %s", zipcode, provider, exc)
                continue
            except Exception as exc:
                log.warning("bundle %s/%s: %s", zipcode, provider, exc)
                continue
            got_any = True
            for extinf, stream in entries:
                m3u_buf.append((extinf, stream))
            for channel in tv.findall("channel"):
                base = base_id(channel.get("id") or "")
                if base in ids:
                    channel.set("id", base)
                    ET.indent(channel, space="  ")
                    chan_buf.append((base, ET.tostring(channel, encoding="unicode")))
            for prog in tv.findall("programme"):
                base = base_id(prog.get("channel") or "")
                if base in ids:
                    prog.set("channel", base)
                    prog_buf.append(((base, prog.get("start")),
                                     ET.tostring(prog, encoding="unicode")))
        with lock:
            if got_any:
                ok_zips.add(zipcode)
            else:
                fail_zips.add(zipcode)
            for extinf, stream in m3u_buf:
                if stream in seen_stream:
                    continue
                seen_stream.add(stream)
                ordered.append((extinf, stream))
                match = TVG_ID.search(extinf)
                if match and match.group(1):
                    channel_ids.add(base_id(match.group(1)))
            for cid, xml in chan_buf:
                if cid not in channels:
                    channels[cid] = xml
            for key, xml in prog_buf:
                if key in seen_prog:
                    continue
                seen_prog.add(key)
                cid_b = key[0].encode("utf-8")
                xml_b = xml.encode("utf-8")
                spool.write(struct.pack("<I", len(cid_b)) + cid_b)
                spool.write(struct.pack("<I", len(xml_b)) + xml_b)

    try:
        pending = list(zips)
        for rnd in range(1, rounds + 1):
            fail_zips.clear()
            run_pool(pending, work, workers, f"round {rnd}")
            pending = sorted(fail_zips)
            if not pending:
                break
            if rnd < rounds:
                wait = min(300, 15 * 2 ** (rnd - 1))
                log.warning("%d/%d ZIPs incomplete; retrying in %ds",
                            len(pending), len(zips), wait)
                time.sleep(wait)

        total = len(zips)
        success = len(ok_zips)
        if success == 0 or not ordered:
            log.error("no usable data this run (%d/%d ZIPs); keeping existing outputs",
                      success, total)
            return False
        if success < total * min_success:
            log.error("only %d/%d ZIPs usable (<%d%%); keeping existing outputs",
                      success, total, int(min_success * 100))
            return False
        previous = _count_extinf(m3u_path)
        if previous and len(ordered) < previous * min_ratio:
            log.error("new m3u has %d entries vs previous %d (<%d%%); keeping previous",
                      len(ordered), previous, int(min_ratio * 100))
            return False

        spool.flush()
        m3u_tmp = m3u_path + ".tmp"
        xml_tmp = xml_path + ".tmp"
        with open(m3u_tmp, "w", encoding="utf-8", newline="\n") as out:
            out.write("#EXTM3U\n")
            for extinf, stream in ordered:
                out.write(extinf + "\n")
                out.write(stream + "\n")

        kept_channels = 0
        kept_progs = 0
        with open(xml_tmp, "wb") as out:
            out.write(b'<?xml version="1.0" encoding="UTF-8"?>\n')
            out.write(b'<tv generator-info-name="build_us_playlists">\n')
            for cid in sorted(channels):
                if cid in channel_ids:
                    out.write(("  " + channels[cid] + "\n").encode("utf-8"))
                    kept_channels += 1
            spool.seek(0)
            while True:
                header = spool.read(4)
                if len(header) < 4:
                    break
                cid = read_exact(spool, struct.unpack("<I", header)[0]).decode("utf-8")
                xlen = struct.unpack("<I", read_exact(spool, 4))[0]
                data = read_exact(spool, xlen)
                if cid in channel_ids:
                    out.write(data)
                    out.write(b"\n")
                    kept_progs += 1
            out.write(b"</tv>\n")

        if not _m3u_ok(m3u_tmp) or not _xml_ok(xml_tmp):
            log.error("new output failed validation; keeping existing outputs")
            for tmp in (m3u_tmp, xml_tmp):
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
            return False

        os.replace(m3u_tmp, m3u_path)
        os.replace(xml_tmp, xml_path)
        log.info("wrote %s: %d entries, %d channel IDs", m3u_path, len(ordered), len(channel_ids))
        log.info("wrote %s: %d channels, %d programmes", xml_path, kept_channels, kept_progs)
        log.info("done: %d/%d ZIPs complete", success, total)
        return True
    finally:
        spool.close()
        try:
            os.unlink(spool.name)
        except OSError:
            pass


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--zip", action="append", default=[], help="only these ZIPs (repeatable)")
    ap.add_argument("--limit", type=int, help="cap number of ZIPs (smoke test)")
    ap.add_argument("--zip-csv", help="local all_us_zipcodes.csv instead of downloading")
    ap.add_argument("--zips-file", help="file with one ZIP per line (hand-picked list)")
    ap.add_argument("--sample", choices=["embedded", "none", "county", "city", "state", "prefix"],
                    default="embedded",
                    help="ZIP set: 'embedded' (default, ~500 top cities), 'none' (all), "
                         "or one per county/city/state/prefix")
    ap.add_argument("--providers", default=",".join(DEFAULT_PROVIDERS),
                    help="comma-separated providers (default us-live-community,big-150; "
                         "directv-stream and youtube-tv add 0 unique channels)")
    ap.add_argument("--workers", type=int, default=8, help="parallel downloads (default 8)")
    ap.add_argument("--m3u", default="combined.m3u", help="output m3u path")
    ap.add_argument("--xml", default="combined.xml", help="output XMLTV path")
    ap.add_argument("--rounds", type=int, default=3, help="retry rounds for failed ZIPs (default 3)")
    ap.add_argument("--min-success", type=float, default=0.5,
                    help="min fraction of ZIPs that must succeed to replace outputs (default 0.5)")
    ap.add_argument("--min-ratio", type=float, default=0.5,
                    help="min new/old entry ratio to replace outputs (default 0.5)")
    ap.add_argument("--log", default=".build_us_playlists.log", help="log file path")
    ap.add_argument("--lock", default=".build_us_playlists.lock", help="run lock file path")
    ap.add_argument("--strict", action="store_true",
                    help="exit non-zero on failure (default: always exit 0)")
    args = ap.parse_args()

    setup_logging(args.log)

    providers = [p.strip() for p in args.providers.split(",") if p.strip()]
    bad = [p for p in providers if p not in PROVIDERS]
    if bad:
        log.error("unknown providers: %s; valid: %s", bad, PROVIDERS)
        return 1 if args.strict else 0

    lock_fh = None
    if fcntl is not None:
        try:
            lock_fh = open(args.lock, "w")
            fcntl.flock(lock_fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            log.warning("another run holds %s; exiting", args.lock)
            return 0

    try:
        zips = get_zips(args.zip_csv, args.zip, args.zips_file, args.sample)
        if args.limit:
            zips = zips[:args.limit]
        if not zips:
            log.error("no ZIP codes resolved")
            return 1 if args.strict else 0
        log.info("%d ZIPs, %d providers (%s), %d workers",
                 len(zips), len(providers), ",".join(providers), args.workers)
        ok = build(zips, args.workers, args.m3u, args.xml, providers,
                   rounds=args.rounds, min_success=args.min_success,
                   min_ratio=args.min_ratio)
        return 0 if ok else (1 if args.strict else 0)
    except KeyboardInterrupt:
        log.warning("interrupted; existing outputs left untouched")
        return 0
    except Exception:
        log.error("unexpected error; existing outputs left untouched:\n%s",
                  traceback.format_exc())
        return 1 if args.strict else 0
    finally:
        if lock_fh is not None:
            try:
                fcntl.flock(lock_fh, fcntl.LOCK_UN)
                lock_fh.close()
            except OSError:
                pass


if __name__ == "__main__":
    sys.exit(main())
