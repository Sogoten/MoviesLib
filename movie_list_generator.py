#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Movie List Generator
---------------------
Σκανάρει έναν φάκελο με ταινίες (υποφακέλους & μεμονωμένα αρχεία βίντεο),
κατεβάζει αυτόματα εξώφυλλα (posters) μέσω του TMDB API, και δημιουργεί ένα
όμορφο, dark-mode HTML αρχείο (movies_list.html) με instant search.
Κλικ σε μια ταινία ανοίγει το αρχείο (ή τον φάκελο) με τον default player/
file explorer του συστήματος — καμία αναπαραγωγή μέσα στον browser, άρα
κανένα πρόβλημα codecs.
"""

import os
import re
import sys
import json
import html
import base64
import shutil
import socket
import hashlib
import platform
import mimetypes
import subprocess
import tempfile
import threading
import webbrowser
import socketserver
import http.server
import urllib.request
import urllib.parse
import urllib.error
from pathlib import Path
from datetime import datetime

# Επεκτάσεις αρχείων βίντεο που αναγνωρίζουμε
VIDEO_EXTENSIONS = {".mkv", ".mp4", ".avi", ".m4v", ".mov"}

# Επεκτάσεις που αγνοούμε ρητά (υπότιτλοι, info αρχεία κλπ.)
IGNORED_EXTENSIONS = {".srt", ".sub", ".idx", ".nfo", ".txt", ".jpg", ".jpeg",
                       ".png", ".url", ".ini", ".db"}

CONFIG_FILENAME = "config.json"
POSTERS_DIRNAME = "posters"
TMDB_SEARCH_URL = "https://api.themoviedb.org/3/search/movie"
TMDB_IMAGE_BASE = "https://image.tmdb.org/t/p/w500"
OMDB_API_URL = "https://www.omdbapi.com/"
VLC_PROTOCOL_NAME = "playvlc"
VLC_HANDLER_FILENAME = "vlc_handler.py"
VLC_REG_FILENAME = "register_vlc_protocol.reg"

# Λέξεις/tags που καθαρίζουμε από τα ονόματα πριν κάνουμε αναζήτηση τίτλου
JUNK_PATTERNS = [
    r"\b(2160p|1080p|720p|480p|4k|uhd)\b",
    r"\b(bluray|blu-ray|bdrip|brrip|webrip|web-?dl|hdtv|dvdrip|dvdscr|hdrip|remux)\b",
    r"\b(x264|x265|h264|h265|hevc|avc)\b",
    r"\b(aac\d*|ac3|dts(-hd)?|atmos|truehd|mp3|5\.1|7\.1|2\.0)\b",
    r"\b(yify|yts(\.\w+)?|rarbg|ettv|evo|sparks)\b",
    r"\b(extended|unrated|directors[\s.]?cut|remastered|repack|proper|internal|limited)\b",
]


# ---------------------------------------------------------------------------
# Βοηθητικές συναρτήσεις γενικής χρήσης
# ---------------------------------------------------------------------------

def clean_path(raw_path: str) -> str:
    """Καθαρίζει τη διαδρομή από κενά και εισαγωγικά (π.χ. από drag & drop)."""
    path = raw_path.strip()
    if len(path) >= 2 and path[0] == path[-1] and path[0] in ("'", '"'):
        path = path[1:-1]
    return path.strip()


def app_dir() -> str:
    """Φάκελος του exe (frozen/PyInstaller) ή του script (κανονική εκτέλεση)."""
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


def human_size(num_bytes: int) -> str:
    """Μετατρέπει bytes σε ανθρώπινα αναγνώσιμη μορφή (KB, MB, GB)."""
    step = 1024.0
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if num_bytes < step:
            return f"{num_bytes:.1f} {unit}"
        num_bytes /= step
    return f"{num_bytes:.1f} PB"


def to_file_uri(path: str) -> str:
    """Μετατρέπει ένα absolute path (αρχείο ή φάκελο) σε file:// URI."""
    try:
        return Path(path).resolve().as_uri()
    except (ValueError, OSError):
        return ""


def get_folder_size(folder_path: str) -> int:
    """Υπολογίζει το συνολικό μέγεθος ενός φακέλου (αναδρομικά), αγνοώντας σφάλματα."""
    total = 0
    for dirpath, _, filenames in os.walk(folder_path):
        for f in filenames:
            fp = os.path.join(dirpath, f)
            try:
                total += os.path.getsize(fp)
            except OSError:
                continue
    return total


def find_collection_videos(folder_path: str):
    """Επιστρέφει λίστα με τα absolute paths όλων των video αρχείων (αναδρομικά) σε έναν φάκελο."""
    videos = []
    for dirpath, _, filenames in os.walk(folder_path):
        for f in filenames:
            if os.path.splitext(f)[1].lower() in VIDEO_EXTENSIONS:
                videos.append(os.path.join(dirpath, f))
    return videos


def to_vlc_uri(path: str) -> str:
    """Μετατρέπει ένα absolute path σε playvlc:// URI (custom protocol -> VLC)."""
    abs_path = str(Path(path).resolve())
    return f"{VLC_PROTOCOL_NAME}://" + urllib.parse.quote(abs_path, safe="")


# ---------------------------------------------------------------------------
# Config (αποθήκευση TMDB API key ώστε να μη ζητείται κάθε φορά)
# ---------------------------------------------------------------------------

def load_config(script_dir: str) -> dict:
    path = os.path.join(script_dir, CONFIG_FILENAME)
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except (OSError, json.JSONDecodeError):
            return {}
    return {}


def save_config(script_dir: str, config: dict) -> None:
    path = os.path.join(script_dir, CONFIG_FILENAME)
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(config, f, ensure_ascii=False, indent=2)
    except OSError:
        pass


def get_api_key(script_dir: str) -> str:
    """Διαβάζει το TMDB API key από το config, ή το ζητάει μία φορά από τον χρήστη."""
    config = load_config(script_dir)
    api_key = (config.get("tmdb_api_key") or "").strip()
    if api_key:
        return api_key

    print("\n🔑 Για αυτόματα εξώφυλλα ταινιών χρειάζεται ένα δωρεάν TMDB API key.")
    print("   Δημιούργησέ το εδώ: https://www.themoviedb.org/settings/api")
    api_key = input("   Επικόλλησε το API key (ή Enter για παράλειψη εξωφύλλων): ").strip()

    if api_key:
        config["tmdb_api_key"] = api_key
        save_config(script_dir, config)
    return api_key


def get_omdb_api_key(script_dir: str) -> str:
    """Διαβάζει το OMDb API key από το config, ή το ζητάει μία φορά (προαιρετικό)."""
    config = load_config(script_dir)
    api_key = (config.get("omdb_api_key") or "").strip()
    if api_key:
        return api_key

    print("\n🔑 (Προαιρετικό) Για IMDb rating & fallback εξώφυλλα χρειάζεται ένα δωρεάν OMDb API key.")
    print("   Δημιούργησέ το εδώ: https://www.omdbapi.com/apikey.aspx")
    api_key = input("   Επικόλλησε το OMDb API key (ή Enter για παράλειψη): ").strip()

    if api_key:
        config["omdb_api_key"] = api_key
        save_config(script_dir, config)
    return api_key


# ---------------------------------------------------------------------------
# Εξαγωγή τίτλου/έτους από όνομα αρχείου/φακέλου + αναζήτηση εξωφύλλου
# ---------------------------------------------------------------------------

def guess_title_year(raw_name: str):
    """Καθαρίζει ένα filename/foldername σε (τίτλος, έτος) για αναζήτηση στο TMDB.

    Στρατηγική: τα release names ακολουθούν σχεδόν πάντα το πρότυπο
    'Τίτλος.Χρονιά.οτιδήποτε-GROUP' (ποιότητα, codec, ήχος, group κλπ).
    Αντί να προσπαθούμε να αναγνωρίσουμε ΚΑΘΕ πιθανό tag (ατελείωτη λίστα),
    κρατάμε μόνο ό,τι υπάρχει ΠΡΙΝ την πρώτη χρονιά — πολύ πιο αξιόπιστο.
    """
    root, ext = os.path.splitext(raw_name)
    if ext.lower() in VIDEO_EXTENSIONS or ext.lower() in IGNORED_EXTENSIONS:
        name = root
    else:
        name = raw_name

    # Αφαιρούμε έναν πιθανό αρχικό αριθμό ταξινόμησης (π.χ. "1.", "10.", "2 - ")
    # που βάζει ο χρήστης στην αρχή του ονόματος φακέλου για σειρά στον Explorer.
    name = re.sub(r"^\d{1,3}[\.\)\-_]+\s*", "", name)

    name = name.replace(".", " ").replace("_", " ")

    year_matches = list(re.finditer(r"(?<!\d)(19\d{2}|20\d{2})(?!\d)", name))
    year_match = year_matches[-1] if year_matches else None
    year = year_match.group(1) if year_match else None

    if year_match and year_match.start() >= 2:
        title = name[:year_match.start()]
    else:
        # Δεν βρέθηκε χρονιά (ή είναι στην αρχή, π.χ. ταινία με αριθμό ως τίτλο):
        # fallback στο παλιό caθάρισμα με γνωστά junk tags σε όλο το string.
        title = name
        for pattern in JUNK_PATTERNS:
            title = re.sub(pattern, " ", title, flags=re.IGNORECASE)

    title = re.sub(r"[\(\[\{]", " ", title)
    title = re.sub(r"[-_]+", " ", title)
    title = re.sub(r"\s+", " ", title).strip(" -.([{")

    if len(title) < 2:
        # Ασφαλές fallback αν το κόψιμο άφησε πολύ λίγα χαρακτήρες
        fallback = name
        for pattern in JUNK_PATTERNS:
            fallback = re.sub(pattern, " ", fallback, flags=re.IGNORECASE)
        fallback = re.sub(r"[-_]+", " ", fallback)
        title = re.sub(r"\s+", " ", fallback).strip(" -.([{")

    return title.strip(), year


def fetch_tmdb_info(api_key: str, title: str, year: str):
    """
    Ρωτάει το TMDB API και επιστρέφει dict {'poster_url', 'title', 'year'} από το
    πρώτο αποτέλεσμα (ο επίσημος τίτλος/έτος χρησιμοποιούνται στο HTML, ΟΧΙ για
    μετονομασία φακέλων), ή None αν αποτύχει η αναζήτηση.
    """
    if not api_key or not title:
        return None

    params = {"api_key": api_key, "query": title}
    if year:
        params["year"] = year

    url = TMDB_SEARCH_URL + "?" + urllib.parse.urlencode(params)

    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        if e.code == 401:
            print("      ❌ Το TMDB API key απορρίφθηκε (401 Unauthorized). Έλεγξε ότι έβαλες το σωστό 'API Key', όχι το 'Read Access Token'.")
        else:
            print(f"      ❌ TMDB HTTP error {e.code}: {e.reason}")
        return None
    except (urllib.error.URLError, OSError, json.JSONDecodeError, TimeoutError) as e:
        print(f"      ❌ Πρόβλημα σύνδεσης με το TMDB: {e}")
        return None

    results = data.get("results") or []
    if not results:
        print("      ⚠️  Καμία αντιστοίχιση στο TMDB για αυτόν τον τίτλο.")
        return None

    best = results[0]
    release_date = best.get("release_date") or ""

    return {
        "poster_url": TMDB_IMAGE_BASE + best["poster_path"] if best.get("poster_path") else None,
        "title": best.get("title") or None,
        "year": release_date[:4] if release_date else None,
    }


def fetch_omdb_data(api_key: str, title: str, year: str):
    """
    Ρωτάει το OMDb API και επιστρέφει dict {'rating': str|None, 'poster_url': str|None},
    ή None αν αποτύχει εντελώς η κλήση.
    """
    if not api_key or not title:
        return None

    params = {"apikey": api_key, "t": title}
    if year:
        params["y"] = year

    url = OMDB_API_URL + "?" + urllib.parse.urlencode(params)

    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        if e.code == 401:
            print("      ❌ Το OMDb API key απορρίφθηκε (401 Unauthorized).")
        else:
            print(f"      ❌ OMDb HTTP error {e.code}: {e.reason}")
        return None
    except (urllib.error.URLError, OSError, json.JSONDecodeError, TimeoutError) as e:
        print(f"      ❌ Πρόβλημα σύνδεσης με το OMDb: {e}")
        return None

    if data.get("Response") == "False":
        return None

    rating = data.get("imdbRating")
    if not rating or rating == "N/A":
        rating = None

    poster_url = data.get("Poster")
    if not poster_url or poster_url == "N/A":
        poster_url = None

    omdb_title = data.get("Title")
    if not omdb_title or omdb_title == "N/A":
        omdb_title = None

    omdb_year = data.get("Year")
    if not omdb_year or omdb_year == "N/A":
        omdb_year = None
    elif omdb_year:
        omdb_year = omdb_year[:4]  # π.χ. OMDb σειρές δίνουν "2011–2014"

    return {"rating": rating, "poster_url": poster_url, "title": omdb_title, "year": omdb_year}


def download_poster(poster_url: str, dest_path: str) -> bool:
    try:
        req = urllib.request.Request(poster_url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = resp.read()
        with open(dest_path, "wb") as f:
            f.write(data)
        return True
    except (urllib.error.URLError, OSError, TimeoutError):
        return False


def get_movie_info(name: str, tmdb_key: str, omdb_key: str, posters_dir: str):
    """
    Επιστρέφει (poster_relpath, imdb_rating, display_name) για ένα αρχείο/φάκελο ταινίας.
    - poster_relpath: π.χ. 'posters/xxxx.jpg' (κατεβασμένο από TMDB, ή OMDb ως fallback), ή None.
    - imdb_rating: π.χ. '7.8', ή None.
    - display_name: επίσημος τίτλος + '(έτος)' από TMDB/OMDb (π.χ. 'Captain America: The First
      Avenger (2011)'), ΜΟΝΟ για εμφάνιση στο HTML — δεν αγγίζει καθόλου το πραγματικό
      όνομα του φακέλου/αρχείου στον δίσκο. Fallback: ο "μαντεμένος" τίτλος από το όνομα φακέλου.
    Χρησιμοποιεί cache βάσει hash(τίτλος+έτος) ώστε να μην ξαναγίνεται το ίδιο download εξωφύλλου.
    """
    guessed_title, guessed_year = guess_title_year(name)
    if not guessed_title:
        return None, None, name

    cache_key = f"{guessed_title}_{guessed_year or ''}".lower()
    file_hash = hashlib.md5(cache_key.encode("utf-8")).hexdigest()[:16]
    filename = f"{file_hash}.jpg"
    dest_path = os.path.join(posters_dir, filename)
    poster_rel = f"{POSTERS_DIRNAME}/{filename}" if os.path.exists(dest_path) else None

    # Το IMDb rating χρειάζεται πάντα κλήση στο OMDb (ανεξάρτητα από poster cache)
    omdb_data = None
    if omdb_key:
        omdb_data = fetch_omdb_data(omdb_key, guessed_title, guessed_year)
    rating = omdb_data.get("rating") if omdb_data else None

    # Πάντα ρωτάμε το TMDB για μεταδεδομένα (επίσημος τίτλος/έτος), ακόμα κι αν το
    # εξώφυλλο είναι ήδη cached — η αναζήτηση είναι ελαφριά, μόνο το image download παραλείπεται.
    tmdb_data = fetch_tmdb_info(tmdb_key, guessed_title, guessed_year) if tmdb_key else None

    if tmdb_data and tmdb_data.get("title"):
        display_title, display_year = tmdb_data["title"], tmdb_data.get("year") or guessed_year
    elif omdb_data and omdb_data.get("title"):
        display_title, display_year = omdb_data["title"], omdb_data.get("year") or guessed_year
    else:
        display_title, display_year = guessed_title, guessed_year

    display_name = f"{display_title} ({display_year})" if display_year else display_title

    if poster_rel:
        return poster_rel, rating, display_name

    print(f"   🖼️  Αναζήτηση εξωφύλλου: {guessed_title}{' (' + guessed_year + ')' if guessed_year else ''}...")

    poster_url = tmdb_data.get("poster_url") if tmdb_data else None

    if not poster_url and omdb_data:
        poster_url = omdb_data.get("poster_url")
        if poster_url:
            print("      ↪️  Χρήση εξωφύλλου από OMDb (fallback).")

    if not poster_url:
        return None, rating, display_name

    os.makedirs(posters_dir, exist_ok=True)
    if download_poster(poster_url, dest_path):
        return f"{POSTERS_DIRNAME}/{filename}", rating, display_name
    return None, rating, display_name


def quick_movie_info(name: str, posters_dir: str):
    """Χωρίς δίκτυο: τίτλος από το όνομα φακέλου + poster μόνο αν υπάρχει ήδη στο cache."""
    title, year = guess_title_year(name)
    if not title:
        return None, None, name
    key = f"{title}_{year or ''}".lower()
    filename = hashlib.md5(key.encode("utf-8")).hexdigest()[:16] + ".jpg"
    poster = f"{POSTERS_DIRNAME}/{filename}" if os.path.exists(os.path.join(posters_dir, filename)) else None
    return poster, None, (f"{title} ({year})" if year else title)


def pending_paths(library: dict) -> list:
    """Paths στοιχείων που περιμένουν αναζήτηση εξωφύλλου/rating."""
    out = []
    for section in ("collections", "single_files"):
        out += [p for p, it in library[section].items() if not it.get("meta_done", True)]
    return out


def rebuild_html(script_dir: str, library: dict, source_folder: str) -> None:
    cols, files = library_to_lists(library)
    with open(os.path.join(script_dir, "movies_list.html"), "w", encoding="utf-8") as f:
        f.write(generate_html(cols, files, source_folder))


def pick_folder_dialog():
    """Ανοίγει το native παράθυρο επιλογής φακέλου των Windows (μέσω PowerShell).
    Επιστρέφει (path, error). path='' αν ακυρώθηκε. Το αποτέλεσμα περνάει από προσωρινό αρχείο UTF-8
    (όχι από stdout) ώστε να μην εξαρτάται από console/encoding."""
    if platform.system() != "Windows":
        return "", "Το παράθυρο επιλογής φακέλου υποστηρίζεται μόνο σε Windows."

    fd, tmp = tempfile.mkstemp(suffix=".txt")
    os.close(fd)
    ps = (
        "$ErrorActionPreference = 'Stop';"
        "Add-Type -AssemblyName System.Windows.Forms; Add-Type -AssemblyName System.Drawing;"
        "$o = New-Object System.Windows.Forms.Form;"
        "$o.TopMost = $true; $o.ShowInTaskbar = $false; $o.StartPosition = 'Manual';"
        "$o.Location = New-Object System.Drawing.Point(-2000, -2000);"
        "$o.Size = New-Object System.Drawing.Size(1, 1);"
        "$o.Show(); $o.Activate();"
        "$d = New-Object System.Windows.Forms.FolderBrowserDialog;"
        "$d.Description = 'Διάλεξε τον φάκελο με τις ταινίες';"
        "if ($d.ShowDialog($o) -eq 'OK') { "
        "[System.IO.File]::WriteAllText('" + tmp.replace("'", "''") + "', $d.SelectedPath, (New-Object System.Text.UTF8Encoding $false)) };"
        "$o.Close()"
    )
    enc = base64.b64encode(ps.encode("utf-16-le")).decode()
    ps_exe = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"),
                          "System32", "WindowsPowerShell", "v1.0", "powershell.exe")
    if not os.path.exists(ps_exe):
        ps_exe = "powershell"

    path, err = "", ""
    try:
        res = subprocess.run(
            [ps_exe, "-NoProfile", "-STA", "-ExecutionPolicy", "Bypass", "-EncodedCommand", enc],
            capture_output=True, stdin=subprocess.DEVNULL,
            creationflags=0x08000000,  # CREATE_NO_WINDOW
            timeout=900,
        )
        try:
            with open(tmp, "r", encoding="utf-8") as f:
                path = f.read().strip()
        except OSError:
            path = ""
        if not path and res.returncode != 0:
            err = res.stderr.decode("utf-8", errors="replace").strip() or f"PowerShell exit code {res.returncode}"
    except (OSError, subprocess.SubprocessError) as e:
        err = str(e)
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass
    return path, err


# ---------------------------------------------------------------------------
# Σάρωση φακέλου
# ---------------------------------------------------------------------------

def find_vlc_path():
    """Ψάχνει το εκτελέσιμο του VLC σε γνωστές τοποθεσίες Windows, αλλιώς μέσω PATH."""
    candidates = [
        r"C:\Program Files\VideoLAN\VLC\vlc.exe",
        r"C:\Program Files (x86)\VideoLAN\VLC\vlc.exe",
    ]
    for c in candidates:
        if os.path.exists(c):
            return c
    found = shutil.which("vlc")
    return found


VLC_FALLBACK_PATHS = [
    r"C:\Program Files\VideoLAN\VLC\vlc.exe",
    r"C:\Program Files (x86)\VideoLAN\VLC\vlc.exe",
]


def get_vlc_path():
    """VLC path από το config.json (αν υπάρχει ακόμα), αλλιώς γνωστές τοποθεσίες/PATH."""
    p = load_config(app_dir()).get("vlc_path")
    if p and os.path.exists(p):
        return p
    return find_vlc_path()


def open_in_vlc(uri: str) -> None:
    """Καλείται όταν το exe ξεκινάει με όρισμα playvlc://... (κλικ σε ταινία από τον browser)."""
    raw = uri[len(VLC_PROTOCOL_NAME + "://"):]
    path = urllib.parse.unquote(raw.rstrip("/"))
    vlc = get_vlc_path()
    try:
        if vlc:
            subprocess.Popen([vlc, path])
        elif hasattr(os, "startfile"):
            os.startfile(path)
    except OSError:
        pass


def register_vlc_protocol(script_dir: str) -> bool:
    """
    Καταχωρεί αυτόματα το playvlc:// στο registry των Windows (HKCU, χωρίς admin, χωρίς .reg).
    Το command δείχνει στο ίδιο το exe — ένα και μόνο αρχείο κάνει server ΚΑΙ ανοίγει το VLC.
    """
    if platform.system() != "Windows":
        return False
    import winreg

    vlc = find_vlc_path()
    if vlc:
        config = load_config(script_dir)
        if config.get("vlc_path") != vlc:
            config["vlc_path"] = vlc
            save_config(script_dir, config)

    if getattr(sys, "frozen", False):
        command = f'"{sys.executable}" "%1"'
    else:
        pythonw = sys.executable.replace("python.exe", "pythonw.exe")
        if not os.path.exists(pythonw):
            pythonw = sys.executable
        command = f'"{pythonw}" "{os.path.abspath(__file__)}" "%1"'

    base = rf"Software\Classes\{VLC_PROTOCOL_NAME}"
    try:
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, base) as k:
            winreg.SetValueEx(k, "", 0, winreg.REG_SZ, "URL:PlayVLC Protocol")
            winreg.SetValueEx(k, "URL Protocol", 0, winreg.REG_SZ, "")
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, base + r"\shell\open\command") as k:
            winreg.SetValueEx(k, "", 0, winreg.REG_SZ, command)
    except OSError:
        return False
    return True


def scan_entry(full_path: str, tmdb_key: str, omdb_key: str, posters_dir: str, fetch: bool = True):
    """Επιστρέφει ("collection"|"single", item) για έναν φάκελο/αρχείο ταινίας, ή None αν αγνοείται.
    fetch=False: γρήγορη σάρωση χωρίς δίκτυο (meta_done=False, τα εξώφυλλα/ratings έρχονται μετά)."""
    entry = os.path.basename(full_path.rstrip("\\/"))

    if os.path.isdir(full_path):
        size = get_folder_size(full_path)
        videos_inside = find_collection_videos(full_path)
        if fetch:
            poster, rating, display_name = get_movie_info(entry, tmdb_key, omdb_key, posters_dir)
        else:
            poster, rating, display_name = quick_movie_info(entry, posters_dir)

        if len(videos_inside) == 1:
            href = to_vlc_uri(videos_inside[0])
            stream_path = os.path.abspath(videos_inside[0])
        else:
            href = to_file_uri(full_path)
            stream_path = None  # πολλά videos μέσα: δεν ξέρουμε ποιο να παίξουμε στο κινητό

        return "collection", {
            "name": entry,
            "display_name": display_name,
            "path": os.path.abspath(full_path),
            "size": human_size(size),
            "href": href,
            "stream_path": stream_path,
            "poster": poster,
            "rating": rating,
            "video_count": len(videos_inside),
            "meta_done": fetch,
        }

    if os.path.isfile(full_path):
        ext = os.path.splitext(entry)[1].lower()
        if ext not in VIDEO_EXTENSIONS:
            return None
        try:
            size = os.path.getsize(full_path)
        except OSError:
            size = 0
        if fetch:
            poster, rating, display_name = get_movie_info(entry, tmdb_key, omdb_key, posters_dir)
        else:
            poster, rating, display_name = quick_movie_info(entry, posters_dir)
        return "single", {
            "name": entry,
            "display_name": display_name,
            "path": os.path.abspath(full_path),
            "size": human_size(size),
            "href": to_vlc_uri(full_path),
            "stream_path": os.path.abspath(full_path),
            "poster": poster,
            "rating": rating,
            "meta_done": fetch,
        }

    return None


def scan_folder(base_path: str, tmdb_key: str, omdb_key: str, posters_dir: str):
    """
    Σαρώνει τον φάκελο-βάση και επιστρέφει δύο λίστες:
    - collections: υποφάκελοι (θεωρούνται ταινίες/συλλογές)
    - single_files: μεμονωμένα αρχεία βίντεο στη ρίζα του φακέλου
    """
    collections, single_files = [], []

    try:
        entries = os.listdir(base_path)
    except (FileNotFoundError, NotADirectoryError, PermissionError) as e:
        print(f"\n❌ Σφάλμα πρόσβασης στον φάκελο: {e}")
        sys.exit(1)

    for entry in entries:
        try:
            result = scan_entry(os.path.join(base_path, entry), tmdb_key, omdb_key, posters_dir)
        except OSError as e:
            print(f"⚠️  Παράλειψη '{entry}' λόγω σφάλματος: {e}")
            continue
        if not result:
            continue
        kind, item = result
        (collections if kind == "collection" else single_files).append(item)

    collections.sort(key=lambda x: x["name"].lower())
    single_files.sort(key=lambda x: x["name"].lower())
    return collections, single_files


# ---------------------------------------------------------------------------
# Μόνιμη βιβλιοθήκη (συγχώνευση αποτελεσμάτων από πολλαπλές σαρώσεις)
# ---------------------------------------------------------------------------

LIBRARY_FILENAME = "library.json"


def load_library(script_dir: str) -> dict:
    """Φορτώνει την υπάρχουσα βιβλιοθήκη (όλες οι ταινίες από προηγούμενες σαρώσεις)."""
    path = os.path.join(script_dir, LIBRARY_FILENAME)
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
                return {
                    "collections": data.get("collections", {}),
                    "single_files": data.get("single_files", {}),
                }
        except (OSError, json.JSONDecodeError):
            pass
    return {"collections": {}, "single_files": {}}


def save_library(script_dir: str, library: dict) -> None:
    path = os.path.join(script_dir, LIBRARY_FILENAME)
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(library, f, ensure_ascii=False, indent=2)
    except OSError as e:
        print(f"⚠️  Δεν ήταν δυνατή η αποθήκευση της βιβλιοθήκης: {e}")


def merge_scan_into_library(library: dict, collections: list, single_files: list) -> None:
    """
    Ενημερώνει τη βιβλιοθήκη (in-place) με τα αποτελέσματα μιας νέας σάρωσης.
    Κάθε στοιχείο αναγνωρίζεται μοναδικά από το απόλυτο path του, οπότε:
    - Νέα στοιχεία προστίθενται.
    - Στοιχεία που ξανασαρώθηκαν ενημερώνονται (π.χ. αν άλλαξε το μέγεθος).
    - Στοιχεία από ΑΛΛΟΥΣ φακέλους που δεν αγγίχτηκαν τώρα παραμένουν ως έχουν.
    """
    for item in collections:
        library["collections"][item["path"]] = item
    for item in single_files:
        library["single_files"][item["path"]] = item


def prune_missing(library: dict) -> None:
    """Αφαιρεί από τη βιβλιοθήκη στοιχεία των οποίων το path δεν υπάρχει πια στον δίσκο."""
    for section in ("collections", "single_files"):
        stale = [p for p in library[section] if not os.path.exists(p)]
        for p in stale:
            del library[section][p]


def library_to_lists(library: dict):
    """Μετατρέπει τη βιβλιοθήκη (dict keyed by path) σε ταξινομημένες λίστες για το HTML."""
    collections = sorted(library["collections"].values(), key=lambda x: x["name"].lower())
    single_files = sorted(library["single_files"].values(), key=lambda x: x["name"].lower())
    return collections, single_files


# ---------------------------------------------------------------------------
# Δημιουργία HTML
# ---------------------------------------------------------------------------

def build_card(name: str, size: str, href: str, poster_rel, icon: str, path: str = "", video_count=None, rating=None, stream_path=None) -> str:
    """Δημιουργεί το HTML για ένα poster-style card (φάκελος ή αρχείο)."""
    safe_name = html.escape(name)
    safe_href = html.escape(href)
    safe_path = html.escape(path)

    img_html = ""
    if poster_rel:
        img_html = f'<img src="{html.escape(poster_rel)}" alt="" loading="lazy" onerror="this.remove()">'

    count_html = ""
    if video_count is not None:
        label = "αρχείο" if video_count == 1 else "αρχεία"
        count_html = f'<div class="count-badge">{video_count} {label}</div>'

    rating_html = ""
    if rating:
        rating_html = f'<div class="rating-badge">⭐ {html.escape(rating)}</div>'

    phone_html = ""
    if stream_path:
        watch_url = (
            "/watch?src=" + urllib.parse.quote(stream_path, safe="")
            + "&title=" + urllib.parse.quote(name, safe="")
        )
        phone_html = f'<button class="phone-btn" type="button" title="Play στο κινητό (μέσω τοπικού server)" data-watch="{html.escape(watch_url)}">📱</button>'

    return f"""
        <a class="card" data-name="{safe_name.lower()}" data-path="{safe_path}" href="{safe_href}" target="_blank" rel="noopener">
            <button class="delete-btn" type="button" title="Διαγραφή από τη λίστα" data-path="{safe_path}">✕</button>
            {phone_html}
            <div class="poster">
                <span class="poster-fallback">{icon}</span>
                {img_html}
                {count_html}
                {rating_html}
                <div class="play-overlay">▶</div>
            </div>
            <div class="card-body">
                <div class="card-title" title="{safe_name}">{safe_name}</div>
                <div class="card-size">{size}</div>
            </div>
        </a>"""


def generate_html(collections, single_files, source_folder: str) -> str:
    """Δημιουργεί το πλήρες περιεχόμενο του HTML αρχείου."""

    collections_html = "".join(
        build_card(c.get("display_name") or c["name"], c["size"], c["href"], c["poster"], "📁", path=c.get("path", ""),
                   video_count=c["video_count"], rating=c.get("rating"), stream_path=c.get("stream_path"))
        for c in collections
    ) if collections else '<p class="empty-msg">Δεν βρέθηκαν φάκελοι/συλλογές.</p>'

    files_html = "".join(
        build_card(f.get("display_name") or f["name"], f["size"], f["href"], f["poster"], "🎬", path=f.get("path", ""),
                   rating=f.get("rating"), stream_path=f.get("stream_path"))
        for f in single_files
    ) if single_files else '<p class="empty-msg">Δεν βρέθηκαν μεμονωμένα αρχεία βίντεο.</p>'

    pending = [it["path"] for it in list(collections) + list(single_files) if not it.get("meta_done", True)]

    generated_on = datetime.now().strftime("%d/%m/%Y %H:%M")
    safe_source = html.escape(source_folder)

    return f"""<!DOCTYPE html>
<html lang="el">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>🎬 Λίστα Ταινιών</title>
<style>
    :root {{
        --bg-main: #121212;
        --bg-card: #1e1e1e;
        --bg-card-hover: #2a2a2e;
        --bg-header: #181818;
        --accent: #6c5ce7;
        --accent-2: #00cec9;
        --text-main: #f1f1f1;
        --text-secondary: #9a9a9a;
        --border-color: #2f2f2f;
        --radius: 14px;
    }}

    * {{ box-sizing: border-box; }}

    body {{
        margin: 0;
        font-family: 'Inter', 'Segoe UI', system-ui, -apple-system, sans-serif;
        background: var(--bg-main);
        color: var(--text-main);
        min-height: 100vh;
    }}

    header {{
        background: linear-gradient(135deg, var(--bg-header), #1f1f26);
        padding: 40px 24px 28px;
        text-align: center;
        border-bottom: 1px solid var(--border-color);
        position: sticky;
        top: 0;
        z-index: 10;
        backdrop-filter: blur(6px);
    }}

    header h1 {{
        margin: 0 0 6px;
        font-size: 2.1rem;
        letter-spacing: -0.5px;
        background: linear-gradient(90deg, var(--accent), var(--accent-2));
        -webkit-background-clip: text;
        background-clip: text;
        color: transparent;
    }}

    header .subtitle {{
        color: var(--text-secondary);
        font-size: 0.9rem;
        margin-bottom: 10px;
        word-break: break-all;
    }}

    header .browser-note {{
        color: var(--text-secondary);
        font-size: 0.76rem;
        margin-bottom: 22px;
        opacity: 0.8;
    }}

    .search-wrapper {{
        max-width: 560px;
        margin: 0 auto;
        position: relative;
    }}

    #searchInput {{
        width: 100%;
        padding: 14px 20px 14px 46px;
        font-size: 1rem;
        border-radius: 999px;
        border: 1px solid var(--border-color);
        background: #232327;
        color: var(--text-main);
        outline: none;
        transition: border-color 0.25s ease, box-shadow 0.25s ease;
    }}

    #searchInput:focus {{
        border-color: var(--accent);
        box-shadow: 0 0 0 3px rgba(108, 92, 231, 0.25);
    }}

    .search-icon {{
        position: absolute;
        left: 18px;
        top: 50%;
        transform: translateY(-50%);
        opacity: 0.6;
        pointer-events: none;
    }}

    main {{
        max-width: 1200px;
        margin: 0 auto;
        padding: 32px 24px 60px;
    }}

    .section {{ margin-bottom: 44px; }}

    .section-header {{
        display: flex;
        align-items: center;
        gap: 12px;
        margin-bottom: 18px;
    }}

    .section-header h2 {{
        font-size: 1.25rem;
        margin: 0;
        font-weight: 600;
    }}

    .badge {{
        background: var(--accent);
        color: white;
        font-size: 0.78rem;
        font-weight: 600;
        padding: 3px 11px;
        border-radius: 999px;
    }}

    .grid {{
        display: grid;
        grid-template-columns: repeat(auto-fill, minmax(160px, 1fr));
        gap: 18px;
    }}

    .card {{
        position: relative;
        display: flex;
        flex-direction: column;
        background: var(--bg-card);
        border: 1px solid var(--border-color);
        border-radius: var(--radius);
        overflow: hidden;
        text-decoration: none;
        color: inherit;
        transition: transform 0.2s ease, border-color 0.2s ease, background 0.2s ease;
    }}

    .delete-btn {{
        position: absolute;
        top: 6px;
        right: 6px;
        width: 24px;
        height: 24px;
        border-radius: 50%;
        border: none;
        background: rgba(0, 0, 0, 0.55);
        color: #fff;
        font-size: 0.75rem;
        line-height: 1;
        cursor: pointer;
        z-index: 5;
        opacity: 0;
        transition: opacity 0.15s ease, background 0.15s ease;
    }}

    .card:hover .delete-btn {{ opacity: 1; }}

    .delete-btn:hover {{
        background: #e74c3c;
    }}

    .phone-btn {{
        position: absolute;
        top: 6px;
        left: 6px;
        width: 24px;
        height: 24px;
        border-radius: 50%;
        border: none;
        background: rgba(0, 0, 0, 0.55);
        color: #fff;
        font-size: 0.75rem;
        line-height: 1;
        cursor: pointer;
        z-index: 5;
        opacity: 0;
        transition: opacity 0.15s ease, background 0.15s ease;
    }}

    .card:hover .phone-btn {{ opacity: 1; }}

    .phone-btn:hover {{
        background: var(--accent);
    }}

    .card:hover {{
        transform: translateY(-4px);
        border-color: var(--accent);
        background: var(--bg-card-hover);
    }}

    .card:hover .play-overlay {{ opacity: 1; }}

    .poster {{
        position: relative;
        width: 100%;
        aspect-ratio: 2 / 3;
        background: linear-gradient(160deg, #2a2a30, #17171a);
        display: flex;
        align-items: center;
        justify-content: center;
        overflow: hidden;
    }}

    .poster-fallback {{
        font-size: 2.6rem;
        opacity: 0.7;
    }}

    .poster img {{
        position: absolute;
        inset: 0;
        width: 100%;
        height: 100%;
        object-fit: cover;
    }}

    .count-badge {{
        position: absolute;
        top: 8px;
        right: 8px;
        background: rgba(0, 0, 0, 0.65);
        color: var(--accent-2);
        font-size: 0.68rem;
        font-weight: 600;
        padding: 3px 9px;
        border-radius: 999px;
        z-index: 2;
    }}

    .rating-badge {{
        position: absolute;
        top: 8px;
        left: 8px;
        background: rgba(0, 0, 0, 0.65);
        color: #f5c518;
        font-size: 0.68rem;
        font-weight: 600;
        padding: 3px 9px;
        border-radius: 999px;
        z-index: 2;
    }}

    .play-overlay {{
        position: absolute;
        inset: 0;
        display: flex;
        align-items: center;
        justify-content: center;
        background: rgba(0, 0, 0, 0.4);
        color: white;
        font-size: 1.8rem;
        opacity: 0;
        transition: opacity 0.2s ease;
        z-index: 1;
    }}

    .card-body {{
        padding: 10px 12px 14px;
    }}

    .card-title {{
        font-weight: 500;
        font-size: 0.85rem;
        line-height: 1.3;
        display: -webkit-box;
        -webkit-line-clamp: 2;
        -webkit-box-orient: vertical;
        overflow: hidden;
    }}

    .card-size {{
        font-size: 0.75rem;
        color: var(--text-secondary);
        margin-top: 5px;
    }}

    .card.hidden {{ display: none; }}

    .empty-msg {{
        color: var(--text-secondary);
        font-style: italic;
    }}

    #noResults {{
        display: none;
        text-align: center;
        color: var(--text-secondary);
        margin-top: 40px;
        font-size: 1rem;
    }}

    footer {{
        text-align: center;
        color: var(--text-secondary);
        font-size: 0.78rem;
        padding: 20px;
        border-top: 1px solid var(--border-color);
    }}

    .restore-link {{
        color: var(--accent-2);
        text-decoration: none;
    }}

    .restore-link:hover {{
        text-decoration: underline;
    }}

    #dropOverlay {{
        display: none;
        position: fixed;
        inset: 0;
        z-index: 100;
        background: rgba(108, 92, 231, 0.88);
        color: #fff;
        font-size: 1.6rem;
        align-items: center;
        justify-content: center;
        text-align: center;
    }}
    #dropOverlay.show {{ display: flex; }}

    .toolbar {{
        display: flex;
        gap: 10px;
        justify-content: center;
        align-items: center;
        flex-wrap: wrap;
        margin-top: 14px;
    }}
    .toolbar button {{
        background: #232327;
        color: var(--text-main);
        border: 1px solid var(--border-color);
        border-radius: 999px;
        padding: 8px 16px;
        font-size: 0.85rem;
        cursor: pointer;
        transition: border-color 0.2s ease, background 0.2s ease;
    }}
    .toolbar button:hover:not(:disabled) {{ border-color: var(--accent); background: #2a2a2e; }}
    .toolbar button:disabled {{ opacity: 0.5; cursor: wait; }}
    #toolStatus {{ color: var(--text-secondary); font-size: 0.82rem; }}
</style>
</head>
<body>

<header>
    <h1>🎬 Λίστα Ταινιών</h1>
    <div class="subtitle">Τελευταία σάρωση: {safe_source} &nbsp;•&nbsp; Ενημερώθηκε στις {generated_on}</div>
    <div class="browser-note">💡 Κλικ σε μια ταινία την ανοίγει με τον default video player σου (π.χ. VLC) — καμία αναπαραγωγή μέσα στη σελίδα.</div>
    <div class="search-wrapper">
        <span class="search-icon">🔍</span>
        <input type="text" id="searchInput" placeholder="Αναζήτηση ταινίας ή αρχείου..." autocomplete="off">
    </div>
    <div class="toolbar" id="toolbar">
        <button id="addFolderBtn" type="button">📂 Προσθήκη φακέλου</button>
        <button id="fetchMetaBtn" type="button">🖼️ Εξώφυλλα &amp; ratings ({len(pending)})</button>
        <button id="quitBtn" type="button" title="Τερματισμός προγράμματος">⏻</button>
        <span id="toolStatus"></span>
    </div>
</header>

<main>
    <section class="section">
        <div class="section-header">
            <h2>📁 Φάκελοι &amp; Συλλογές</h2>
            <span class="badge" id="collectionsBadge">{len(collections)}</span>
        </div>
        <div class="grid" id="collectionsGrid">{collections_html}
        </div>
    </section>

    <section class="section">
        <div class="section-header">
            <h2>📄 Μεμονωμένα Αρχεία</h2>
            <span class="badge" id="filesBadge">{len(single_files)}</span>
        </div>
        <div class="grid" id="filesGrid">{files_html}
        </div>
    </section>

    <div id="noResults">Δεν βρέθηκαν αποτελέσματα 🔎</div>
</main>

<footer>
    Movie List Generator &middot; <span id="totalCount">{len(collections) + len(single_files)}</span> στοιχεία συνολικά
    &middot; <a href="#" id="restoreDeleted" class="restore-link">Επαναφορά διαγραμμένων</a>
</footer>

<div id="dropOverlay"><div id="dropText">📥 Άφησε εδώ ταινίες ή φακέλους</div></div>

<script>
    const searchInput = document.getElementById('searchInput');
    const noResults = document.getElementById('noResults');
    const collectionsBadge = document.getElementById('collectionsBadge');
    const filesBadge = document.getElementById('filesBadge');
    const totalCount = document.getElementById('totalCount');
    const restoreLink = document.getElementById('restoreDeleted');
    const DELETED_KEY = 'movieListDeletedPaths';

    function getDeletedPaths() {{
        try {{
            return JSON.parse(localStorage.getItem(DELETED_KEY) || '[]');
        }} catch (e) {{
            return [];
        }}
    }}

    function saveDeletedPaths(paths) {{
        localStorage.setItem(DELETED_KEY, JSON.stringify(paths));
    }}

    function updateBadges() {{
        const remainingCollections = document.querySelectorAll('#collectionsGrid .card:not(.deleted)').length;
        const remainingFiles = document.querySelectorAll('#filesGrid .card:not(.deleted)').length;
        collectionsBadge.textContent = remainingCollections;
        filesBadge.textContent = remainingFiles;
        totalCount.textContent = remainingCollections + remainingFiles;
    }}

    function applyDeletedOnLoad() {{
        const deleted = new Set(getDeletedPaths());
        if (deleted.size === 0) return;
        document.querySelectorAll('.card').forEach(card => {{
            const path = card.getAttribute('data-path');
            if (path && deleted.has(path)) {{
                card.classList.add('deleted');
                card.style.display = 'none';
            }}
        }});
        updateBadges();
    }}

    document.querySelectorAll('.delete-btn').forEach(btn => {{
        btn.addEventListener('click', (e) => {{
            e.preventDefault();
            e.stopPropagation();
            const path = btn.getAttribute('data-path');
            const card = btn.closest('.card');
            const title = card ? (card.querySelector('.card-title')?.textContent || 'αυτό το στοιχείο') : 'αυτό το στοιχείο';
            if (!confirm(`Αφαίρεση "${{title}}" από τη λίστα; (Το αρχείο ΔΕΝ διαγράφεται από τον δίσκο)`)) return;

            const deleted = getDeletedPaths();
            if (path && !deleted.includes(path)) {{
                deleted.push(path);
                saveDeletedPaths(deleted);
            }}
            if (card) {{
                card.classList.add('deleted');
                card.style.display = 'none';
            }}
            updateBadges();
        }});
    }});

    document.querySelectorAll('.phone-btn').forEach(btn => {{
        btn.addEventListener('click', (e) => {{
            e.preventDefault();
            e.stopPropagation();
            const watchUrl = btn.getAttribute('data-watch');
            if (watchUrl) window.open(watchUrl, '_blank');
        }});
    }});

    restoreLink.addEventListener('click', (e) => {{
        e.preventDefault();
        if (!confirm('Επαναφορά όλων των διαγραμμένων στοιχείων στη λίστα;')) return;
        saveDeletedPaths([]);
        document.querySelectorAll('.card.deleted').forEach(card => {{
            card.classList.remove('deleted');
            card.style.display = '';
        }});
        updateBadges();
    }});

    applyDeletedOnLoad();

    const allCards = Array.from(document.querySelectorAll('.card'));

    searchInput.addEventListener('input', () => {{
        const query = searchInput.value.trim().toLowerCase();
        let visibleCount = 0;

        allCards.forEach(card => {{
            if (card.classList.contains('deleted')) return;
            const name = card.getAttribute('data-name') || '';
            const matches = name.includes(query);
            card.classList.toggle('hidden', !matches);
            if (matches) visibleCount++;
        }});

        noResults.style.display = visibleCount === 0 ? 'block' : 'none';
    }});

    // ---- Drag & drop νέων ταινιών (δουλεύει μόνο μέσω του τοπικού server) ----
    const dropOverlay = document.getElementById('dropOverlay');
    const dropText = document.getElementById('dropText');
    let dragDepth = 0;

    function hasFiles(e) {{
        return e.dataTransfer && Array.from(e.dataTransfer.types || []).includes('Files');
    }}

    async function postAdd(payload) {{
        const r = await fetch('/add', {{
            method: 'POST',
            headers: {{ 'Content-Type': 'application/json' }},
            body: JSON.stringify(payload)
        }});
        if (!r.ok) throw new Error('HTTP ' + r.status);
        return r.json();
    }}

    window.addEventListener('dragenter', (e) => {{
        if (!hasFiles(e)) return;
        e.preventDefault();
        dragDepth++;
        dropText.textContent = '📥 Άφησε εδώ ταινίες ή φακέλους';
        dropOverlay.classList.add('show');
    }});

    window.addEventListener('dragover', (e) => {{
        if (hasFiles(e)) e.preventDefault();
    }});

    window.addEventListener('dragleave', (e) => {{
        if (!hasFiles(e)) return;
        dragDepth = Math.max(0, dragDepth - 1);
        if (dragDepth === 0) dropOverlay.classList.remove('show');
    }});

    window.addEventListener('drop', async (e) => {{
        if (!hasFiles(e)) return;
        e.preventDefault();
        dragDepth = 0;

        // Πρέπει να διαβαστούν συγχρονισμένα, πριν από οποιοδήποτε await
        const names = Array.from(e.dataTransfer.items)
            .map(i => i.webkitGetAsEntry ? i.webkitGetAsEntry() : null)
            .filter(Boolean)
            .map(en => en.name);

        if (names.length === 0) {{ dropOverlay.classList.remove('show'); return; }}
        if (!location.protocol.startsWith('http')) {{
            alert('Το drag & drop δουλεύει μόνο όταν η σελίδα ανοίγει από τον τοπικό server (http://localhost:8765/movies_list.html).');
            dropOverlay.classList.remove('show');
            return;
        }}

        dropText.textContent = '⏳ Προσθήκη... (κατεβάζω εξώφυλλα)';
        try {{
            let res = await postAdd({{ names: names }});
            const paths = [];
            for (const n of (res.missing || [])) {{
                const p = prompt('Δεν βρέθηκε το "' + n + '" στους γνωστούς φακέλους.\\nΕπικόλλησε το πλήρες path (Shift + δεξί κλικ → Copy as path):');
                if (p) paths.push(p.trim().replace(/^"|"$/g, ''));
            }}
            if (paths.length) res = await postAdd({{ paths: paths }});
            location.reload();
        }} catch (err) {{
            alert('Σφάλμα: ' + err);
            dropOverlay.classList.remove('show');
        }}
    }});

    // ---- Toolbar: προσθήκη φακέλου & εξώφυλλα (μόνο μέσω τοπικού server) ----
    const PENDING = {json.dumps(pending)};
    const toolbar = document.getElementById('toolbar');
    const toolStatus = document.getElementById('toolStatus');
    const addFolderBtn = document.getElementById('addFolderBtn');
    const fetchMetaBtn = document.getElementById('fetchMetaBtn');

    if (!location.protocol.startsWith('http')) toolbar.style.display = 'none';
    if (PENDING.length === 0) fetchMetaBtn.style.display = 'none';

    async function api(url, payload) {{
        const r = await fetch(url, {{
            method: 'POST',
            headers: {{ 'Content-Type': 'application/json' }},
            body: JSON.stringify(payload || {{}})
        }});
        if (!r.ok) throw new Error('HTTP ' + r.status);
        return r.json();
    }}

    function setBusy(b) {{
        addFolderBtn.disabled = b;
        fetchMetaBtn.disabled = b;
    }}

    async function runFetch(paths) {{
        setBusy(true);
        for (let i = 0; i < paths.length; i++) {{
            toolStatus.textContent = '🖼️ ' + (i + 1) + '/' + paths.length + ' ...';
            try {{
                const r = await api('/fetch-meta', {{ path: paths[i] }});
                if (r.error) {{ alert(r.error); break; }}
            }} catch (e) {{ console.error(e); }}
        }}
        toolStatus.textContent = 'Ανανέωση...';
        await api('/rebuild');
        location.reload();
    }}

    fetchMetaBtn.addEventListener('click', () => runFetch(PENDING));

    document.getElementById('quitBtn').addEventListener('click', async () => {{
        if (!confirm('Τερματισμός του προγράμματος (server);')) return;
        try {{ await api('/shutdown'); }} catch (e) {{}}
        document.body.innerHTML = '<p style="padding:40px;text-align:center;color:#9a9a9a">Το πρόγραμμα τερμάτισε. Μπορείς να κλείσεις την καρτέλα.</p>';
    }});

    addFolderBtn.addEventListener('click', async () => {{
        setBusy(true);
        try {{
            toolStatus.textContent = '📂 Διάλεξε φάκελο στο παράθυρο των Windows...';
            let pick = await api('/pick-folder');
            if (pick.error) {{
                const m = prompt('Δεν άνοιξε το παράθυρο επιλογής φακέλου.\\nΕπικόλλησε χειροκίνητα το path του φακέλου (Shift + δεξί κλικ → Copy as path):');
                pick.path = m ? m.trim().replace(/^"|"$/g, '') : '';
            }}
            if (!pick.path) {{ toolStatus.textContent = ''; setBusy(false); return; }}

            toolStatus.textContent = '🔎 Σάρωση...';
            const res = await api('/scan-folder', {{ path: pick.path }});
            if (res.error) throw new Error(res.error);

            if (res.pending.length && confirm('Βρέθηκαν ' + res.new + ' νέα στοιχεία. Να ψάξω τώρα εξώφυλλα/ratings για ' + res.pending.length + ';')) {{
                await runFetch(res.pending);
                return;
            }}
            location.reload();
        }} catch (err) {{
            alert('Σφάλμα: ' + err);
            toolStatus.textContent = '';
            setBusy(false);
        }}
    }});
</script>

</body>
</html>"""



# ---------------------------------------------------------------------------
# Τοπικός streaming server (για να παίζουν οι ταινίες στον browser του κινητού)
# ---------------------------------------------------------------------------

STREAM_PORT = 8765
STREAM_CHUNK_SIZE = 1024 * 1024  # 1 MB


def get_lan_ip() -> str:
    """Βρίσκει την τοπική IP του PC στο LAN (χωρίς να στέλνει πραγματικά δεδομένα πουθενά)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


WATCH_PAGE_TEMPLATE = """<!DOCTYPE html>
<html lang="el">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<style>
    html, body {{ margin: 0; padding: 0; background: #000; height: 100%; }}
    video {{ width: 100%; height: 100%; object-fit: contain; background: #000; display: block; }}
    a.back {{
        position: fixed; top: 10px; left: 10px; color: #fff;
        background: rgba(0, 0, 0, 0.6); padding: 6px 12px; border-radius: 6px;
        text-decoration: none; font-family: sans-serif; font-size: 14px; z-index: 10;
    }}
</style>
</head>
<body>
    <a class="back" href="/movies_list.html">← Πίσω στη λίστα</a>
    <video controls autoplay playsinline src="{video_src}"></video>
</body>
</html>"""


LIBRARY_LOCK = threading.Lock()


def known_roots(script_dir: str, library: dict) -> list:
    """Φάκελοι όπου ψάχνουμε τα ονόματα που έγιναν drop: σκαναρισμένοι + γονικοί των ήδη γνωστών ταινιών."""
    roots = list(load_config(script_dir).get("library_roots", []))
    for section in ("collections", "single_files"):
        for p in library[section]:
            parent = os.path.dirname(p)
            if parent not in roots:
                roots.append(parent)
    return roots


def remember_root(script_dir: str, folder: str) -> None:
    config = load_config(script_dir)
    roots = config.get("library_roots", [])
    folder = os.path.abspath(folder)
    if folder not in roots:
        roots.append(folder)
        config["library_roots"] = roots
        save_config(script_dir, config)


class MovieStreamHandler(http.server.BaseHTTPRequestHandler):
    """
    Σερβίρει το movies_list.html, τα posters, και τα ίδια τα video (με πραγματική
    υποστήριξη HTTP Range requests, απαραίτητη ώστε να δουλεύει το seek/scrub στο
    video player του browser). Το script_dir ορίζεται πριν ξεκινήσει ο server.
    """
    script_dir = ""
    tmdb_key = ""
    omdb_key = ""
    source_folder = ""
    protocol_version = "HTTP/1.1"

    def do_GET(self):
        self._handle()

    def do_HEAD(self):
        self._handle()

    def do_POST(self):
        route = urllib.parse.urlsplit(self.path).path
        routes = {
            "/add": self._handle_add,
            "/pick-folder": self._handle_pick_folder,
            "/scan-folder": self._handle_scan_folder,
            "/fetch-meta": self._handle_fetch_meta,
            "/rebuild": self._handle_rebuild,
            "/shutdown": self._handle_shutdown,
        }
        handler = routes.get(route)
        if not handler:
            self.send_error(404, "Not found")
            return
        if self.client_address[0] not in ("127.0.0.1", "::1"):
            self.send_error(403, "Μόνο από το ίδιο PC")
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length) or b"{}")
        except (ValueError, json.JSONDecodeError):
            self.send_error(400, "Bad request")
            return
        handler(body)

    def _send_json(self, obj, status=200):
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _handle_add(self, body):
        with LIBRARY_LOCK:
            library = load_library(self.script_dir)
            posters_dir = os.path.join(self.script_dir, POSTERS_DIRNAME)

            paths = [clean_path(p) for p in body.get("paths", []) if isinstance(p, str)]
            missing = []

            roots = known_roots(self.script_dir, library)
            for name in body.get("names", []):
                if not isinstance(name, str) or not name or name != os.path.basename(name):
                    continue  # αγνοούμε ονόματα με διαδρομές (ασφάλεια)
                found = next((os.path.join(r, name) for r in roots
                              if os.path.exists(os.path.join(r, name))), None)
                if found:
                    paths.append(found)
                else:
                    missing.append(name)

            added = []
            for p in paths:
                if not os.path.exists(p):
                    missing.append(p)
                    continue
                print(f"   ➕ Προσθήκη από drag & drop: {p}")
                try:
                    result = scan_entry(p, self.tmdb_key, self.omdb_key, posters_dir)
                except OSError as e:
                    print(f"⚠️  Παράλειψη '{p}': {e}")
                    continue
                if not result:
                    missing.append(p)
                    continue
                kind, item = result
                if kind == "collection":
                    merge_scan_into_library(library, [item], [])
                else:
                    merge_scan_into_library(library, [], [item])
                added.append(item["display_name"])

            if added:
                save_library(self.script_dir, library)
                rebuild_html(self.script_dir, library, self.source_folder)

        self._send_json({"added": added, "missing": missing})

    def _handle_pick_folder(self, body):
        path, err = pick_folder_dialog()
        if err:
            try:
                with open(os.path.join(self.script_dir, "error.log"), "a", encoding="utf-8") as f:
                    f.write(f"[{datetime.now():%d/%m/%Y %H:%M:%S}] pick-folder: {err}\n")
            except OSError:
                pass
        self._send_json({"path": path, "error": err})

    def _handle_scan_folder(self, body):
        folder = clean_path(body.get("path") or "")
        if not os.path.isdir(folder):
            self._send_json({"error": "Ο φάκελος δεν υπάρχει: " + folder})
            return

        posters_dir = os.path.join(self.script_dir, POSTERS_DIRNAME)
        new_count = 0
        with LIBRARY_LOCK:
            library = load_library(self.script_dir)
            try:
                entries = os.listdir(folder)
            except OSError as e:
                self._send_json({"error": str(e)})
                return

            for entry in entries:
                try:
                    result = scan_entry(os.path.join(folder, entry), "", "", posters_dir, fetch=False)
                except OSError:
                    continue
                if not result:
                    continue
                kind, item = result
                section = "collections" if kind == "collection" else "single_files"
                old = library[section].get(item["path"])
                if old:
                    if old.get("meta_done", True):  # κρατάμε ό,τι είχε ήδη βρεθεί
                        for k in ("display_name", "poster", "rating"):
                            if k in old:
                                item[k] = old[k]
                        item["meta_done"] = True
                else:
                    new_count += 1
                library[section][item["path"]] = item

            prune_missing(library)
            save_library(self.script_dir, library)
            remember_root(self.script_dir, folder)
            MovieStreamHandler.source_folder = folder
            rebuild_html(self.script_dir, library, folder)
            pending = pending_paths(library)

        self._send_json({"new": new_count, "pending": pending})

    def _handle_fetch_meta(self, body):
        path = body.get("path") or ""
        if not self.tmdb_key and not self.omdb_key:
            self._send_json({"error": "Δεν υπάρχουν API keys. Βάλε tmdb_api_key / omdb_api_key στο config.json δίπλα στο exe."})
            return

        with LIBRARY_LOCK:
            library = load_library(self.script_dir)
            section = next((s for s in ("collections", "single_files") if path in library[s]), None)
            if not section:
                self._send_json({"error": "Άγνωστο path"})
                return
            name = library[section][path]["name"]

        # Η κλήση δικτύου γίνεται ΕΞΩ από το lock ώστε να μη μπλοκάρει τα υπόλοιπα
        poster, rating, display_name = get_movie_info(
            name, self.tmdb_key, self.omdb_key, os.path.join(self.script_dir, POSTERS_DIRNAME))

        with LIBRARY_LOCK:
            library = load_library(self.script_dir)
            item = library[section].get(path)
            if item:
                item.update({"poster": poster, "rating": rating,
                             "display_name": display_name, "meta_done": True})
                save_library(self.script_dir, library)

        self._send_json({"display_name": display_name, "poster": poster, "rating": rating})

    def _handle_shutdown(self, body):
        self._send_json({"ok": True})
        threading.Thread(target=self.server.shutdown, daemon=True).start()

    def _handle_rebuild(self, body):
        with LIBRARY_LOCK:
            library = load_library(self.script_dir)
            rebuild_html(self.script_dir, library, self.source_folder)
        self._send_json({"ok": True})

    def _handle(self):
        parsed = urllib.parse.urlsplit(self.path)
        route = parsed.path
        query = urllib.parse.parse_qs(parsed.query)

        try:
            if route in ("/", "/movies_list.html"):
                self._serve_file(os.path.join(self.script_dir, "movies_list.html"), "text/html; charset=utf-8")
            elif route.startswith("/posters/"):
                self._serve_posters(route)
            elif route == "/watch":
                self._serve_watch_page(query)
            elif route == "/stream":
                self._serve_video(query)
            else:
                self.send_error(404, "Not found")
        except (BrokenPipeError, ConnectionResetError):
            pass  # ο browser έκλεισε τη σύνδεση (π.χ. λόγω seek) - φυσιολογικό, αγνοείται

    def _serve_posters(self, route):
        posters_root = os.path.normpath(os.path.join(self.script_dir, POSTERS_DIRNAME))
        rel = urllib.parse.unquote(route[len("/posters/"):])
        full = os.path.normpath(os.path.join(posters_root, rel))
        if not full.startswith(posters_root) or not os.path.isfile(full):
            self.send_error(404, "Not found")
            return
        content_type = mimetypes.guess_type(full)[0] or "application/octet-stream"
        self._serve_file(full, content_type)

    def _serve_file(self, filepath, content_type):
        """Στέλνει ένα μικρό στατικό αρχείο (HTML/εικόνα) εξ ολοκλήρου, χωρίς Range."""
        if not os.path.isfile(filepath):
            self.send_error(404, "Not found")
            return
        try:
            with open(filepath, "rb") as f:
                data = f.read()
        except OSError:
            self.send_error(404, "Not found")
            return

        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def _serve_watch_page(self, query):
        src = (query.get("src") or [""])[0]
        title = (query.get("title") or ["Ταινία"])[0]
        if not src or not os.path.isfile(src):
            self.send_error(404, "Video not found")
            return

        video_src = "/stream?path=" + urllib.parse.quote(src, safe="")
        page = WATCH_PAGE_TEMPLATE.format(title=html.escape(title), video_src=html.escape(video_src))
        data = page.encode("utf-8")

        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def _serve_video(self, query):
        """Στέλνει ένα video αρχείο με πλήρη υποστήριξη Range requests (seek/scrub)."""
        raw_path = (query.get("path") or [""])[0]
        abspath = urllib.parse.unquote(raw_path)

        if not abspath or not os.path.isfile(abspath):
            self.send_error(404, "Video not found")
            return
        if os.path.splitext(abspath)[1].lower() not in VIDEO_EXTENSIONS:
            self.send_error(403, "Μη υποστηριζόμενος τύπος αρχείου")
            return

        file_size = os.path.getsize(abspath)
        content_type = mimetypes.guess_type(abspath)[0] or "application/octet-stream"

        start, end = 0, file_size - 1
        status = 200
        range_header = self.headers.get("Range")
        if range_header:
            match = re.match(r"bytes=(\d*)-(\d*)", range_header)
            if match:
                start_str, end_str = match.groups()
                if start_str:
                    start = int(start_str)
                if end_str:
                    end = int(end_str)
                status = 206

        if start >= file_size or end >= file_size or start > end:
            self.send_response(416)
            self.send_header("Content-Range", f"bytes */{file_size}")
            self.end_headers()
            return

        length = end - start + 1

        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(length))
        if status == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{file_size}")
        self.end_headers()

        if self.command == "HEAD":
            return

        with open(abspath, "rb") as f:
            f.seek(start)
            remaining = length
            while remaining > 0:
                chunk = f.read(min(STREAM_CHUNK_SIZE, remaining))
                if not chunk:
                    break
                self.wfile.write(chunk)
                remaining -= len(chunk)

    def log_message(self, format, *args):
        pass  # σιωπηλό: αλλιώς κάθε video chunk τυπώνει δική του γραμμή στο console


def start_local_server(script_dir: str, tmdb_key: str = "", omdb_key: str = "",
                       source_folder: str = "", port: int = STREAM_PORT, open_browser: bool = False) -> None:
    """Ξεκινάει τον τοπικό server (blocking, μέχρι Ctrl+C ή το κουμπί ⏻ της σελίδας)."""
    MovieStreamHandler.script_dir = script_dir
    MovieStreamHandler.tmdb_key = tmdb_key
    MovieStreamHandler.omdb_key = omdb_key
    MovieStreamHandler.source_folder = source_folder
    local_url = f"http://localhost:{port}/movies_list.html"

    socketserver.ThreadingTCPServer.allow_reuse_address = True
    try:
        server = socketserver.ThreadingTCPServer(("0.0.0.0", port), MovieStreamHandler)
    except OSError:
        # Τρέχει ήδη άλλο instance: απλά ανοίγουμε τη σελίδα του
        print(f"ℹ️  Η θύρα {port} είναι ήδη σε χρήση (τρέχει ήδη το πρόγραμμα). Άνοιγμα σελίδας.")
        if open_browser:
            webbrowser.open(local_url)
        return
    server.daemon_threads = True

    lan_ip = get_lan_ip()
    print("\n" + "=" * 55)
    print("📡  Ο τοπικός server ξεκίνησε!")
    print(f"   Στο ίδιο PC:      {local_url}")
    print(f"   Στο κινητό (WiFi): http://{lan_ip}:{port}/movies_list.html")
    print("   Άνοιξε το 2ο link στο κινητό (ίδιο WiFi με το PC) και πάτα 📱 σε μια ταινία.")
    print("   Πάτα Ctrl+C εδώ (ή ⏻ στη σελίδα) για να σταματήσει ο server.")
    print("=" * 55)

    if open_browser:
        threading.Timer(0.8, lambda: webbrowser.open(local_url)).start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n🛑 Ο server σταμάτησε.")
    finally:
        server.server_close()


def cli_main():
    print("=" * 55)
    print("🎬  Movie List Generator (CLI)")
    print("=" * 55)

    raw_input_path = input("\nΔώσε τη διαδρομή του φακέλου με τις ταινίες: ")
    folder_path = clean_path(raw_input_path)

    if not folder_path:
        print("❌ Δεν δόθηκε διαδρομή. Έξοδος.")
        sys.exit(1)

    if not os.path.isdir(folder_path):
        print(f"❌ Ο φάκελος δεν υπάρχει ή δεν είναι έγκυρος: {folder_path}")
        sys.exit(1)

    script_dir = app_dir()
    posters_dir = os.path.join(script_dir, POSTERS_DIRNAME)

    register_vlc_protocol(script_dir)

    tmdb_key = get_api_key(script_dir)
    if not tmdb_key:
        print("   ℹ️  Χωρίς TMDB API key: θα χρησιμοποιηθούν εικονίδια αντί για εξώφυλλα.")

    omdb_key = get_omdb_api_key(script_dir)
    if not omdb_key:
        print("   ℹ️  Χωρίς OMDb API key: δεν θα εμφανίζονται IMDb ratings ούτε fallback εξώφυλλα.")

    print(f"\n🔎 Σάρωση φακέλου: {folder_path} ...")
    collections, single_files = scan_folder(folder_path, tmdb_key, omdb_key, posters_dir)

    print(f"   ✔ Βρέθηκαν {len(collections)} φάκελοι/συλλογές.")
    print(f"   ✔ Βρέθηκαν {len(single_files)} μεμονωμένα αρχεία βίντεο.")

    library = load_library(script_dir)
    merge_scan_into_library(library, collections, single_files)
    prune_missing(library)  # αφαιρεί ό,τι δεν υπάρχει πια στον δίσκο (π.χ. διαγραμμένα αρχεία)
    save_library(script_dir, library)
    remember_root(script_dir, folder_path)

    all_collections, all_single_files = library_to_lists(library)
    print(f"   📚 Σύνολο στη βιβλιοθήκη: {len(all_collections)} συλλογές, {len(all_single_files)} μεμονωμένα αρχεία.")

    html_content = generate_html(all_collections, all_single_files, folder_path)

    output_path = os.path.join(script_dir, "movies_list.html")

    try:
        with open(output_path, "w", encoding="utf-8") as f:
            f.write(html_content)
    except OSError as e:
        print(f"❌ Σφάλμα κατά την αποθήκευση του HTML: {e}")
        sys.exit(1)

    print(f"\n✅ Το αρχείο δημιουργήθηκε επιτυχώς: {output_path}")
    print("   Άνοιξέ το με διπλό κλικ!\n")

    choice = input("📡 Θες να ξεκινήσει τώρα ο τοπικός server (streaming στο κινητό); (y/N): ").strip().lower()
    if choice == "y":
        start_local_server(script_dir, tmdb_key, omdb_key, folder_path)


def ensure_std_streams() -> None:
    """Σε --noconsole exe τα sys.stdout/stderr είναι None: τα γυρνάμε σε devnull ώστε να μη σκάει κάποιο print."""
    for name in ("stdout", "stderr"):
        if getattr(sys, name) is None:
            setattr(sys, name, open(os.devnull, "w", encoding="utf-8"))


def main():
    ensure_std_streams()

    # 1) Κλικ σε ταινία από τον browser: το Windows ξεκινά το exe με όρισμα playvlc://...
    if len(sys.argv) > 1 and sys.argv[1].lower().startswith(VLC_PROTOCOL_NAME + "://"):
        open_in_vlc(sys.argv[1])
        return

    # 2) Παλιός διαδραστικός τρόπος (console): movie_list_generator.exe --cli
    if "--cli" in sys.argv:
        cli_main()
        return

    # 3) Κανονική εκκίνηση: server + άνοιγμα browser, όλα τα υπόλοιπα από τη σελίδα
    script_dir = app_dir()
    config = load_config(script_dir)
    tmdb_key = (config.get("tmdb_api_key") or "").strip()
    omdb_key = (config.get("omdb_api_key") or "").strip()

    register_vlc_protocol(script_dir)

    library = load_library(script_dir)
    prune_missing(library)
    save_library(script_dir, library)

    roots = config.get("library_roots") or []
    source_folder = roots[-1] if roots else "—"
    rebuild_html(script_dir, library, source_folder)

    start_local_server(script_dir, tmdb_key, omdb_key, source_folder, open_browser=True)


if __name__ == "__main__":
    main()