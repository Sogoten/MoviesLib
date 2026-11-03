# MoviesLib

Δημιουργεί μια όμορφη, dark-mode λίστα (HTML) με τις ταινίες σου, με εξώφυλλα και instant search.
Σκανάρει φακέλους με ταινίες, κατεβάζει posters και πληροφορίες από TMDB/OMDb, και σε αφήνει να ανοίγεις
κάθε ταινία με ένα κλικ στο VLC.

## Τι κάνει

- Σκανάρει φακέλους με ταινίες (υποφακέλους και μεμονωμένα αρχεία: `.mkv`, `.mp4`, `.avi`, `.m4v`, `.mov`)
- Κατεβάζει εξώφυλλα και στοιχεία ταινιών μέσω TMDB και OMDb
- Φτιάχνει το `movies_list.html` με instant search
- Τρέχει τοπικό server (προεπιλεγμένη θύρα `8765`) και ανοίγει τη λίστα στον browser
- Το κλικ σε ταινία την ανοίγει στο VLC μέσω του custom protocol `playvlc://`, που καταχωρείται
  αυτόματα στο registry των Windows (HKCU, δεν χρειάζεται admin)

## Απαιτήσεις

- Windows
- Python 3 (μόνο για build από τον κώδικα)
- [VLC](https://www.videolan.org/vlc/)
- Δωρεάν API keys από [TMDB](https://www.themoviedb.org/settings/api) και [OMDb](https://www.omdbapi.com/apikey.aspx)

## Ρύθμιση

1. Αντίγραψε το `config.example.json` σε `config.json`.
2. Βάλε τα keys σου:

```json
{
  "tmdb_api_key": "YOUR_TMDB_KEY",
  "omdb_api_key": "YOUR_OMDB_KEY"
}
```

Για το TMDB χρησιμοποίησε το **API Key**, όχι το "Read Access Token".

Το `config.json` δεν ανεβαίνει στο repo (είναι στο `.gitignore`). Μην το κάνεις commit.

Προαιρετικά μπορείς να ορίσεις `vlc_path` στο `config.json` αν το VLC δεν βρίσκεται στις
προεπιλεγμένες διαδρομές. Αλλιώς ανιχνεύεται αυτόματα.

## Build

Τρέξε το `build.bat`. Εγκαθιστά το PyInstaller και φτιάχνει ένα μόνο exe (server και VLC handler μαζί, χωρίς console).
Το αποτέλεσμα μπαίνει στον φάκελο `release/`:

- `movie_list_generator.exe`
- `config.example.json`
- `library.json` και `posters/` (αν υπάρχουν)

Το `config.json` **δεν** αντιγράφεται στο `release/`. Βάλε το δικό σου δίπλα στο exe.

## Χρήση

1. Βάλε το `config.json` με τα keys δίπλα στο `movie_list_generator.exe`.
2. Τρέξε το exe. Ανοίγει ο browser με τη λίστα.
3. Πρόσθεσε τον φάκελο με τις ταινίες σου από τη σελίδα.
4. Κλικ σε ταινία για να ανοίξει στο VLC.

Για τον παλιό διαδραστικό τρόπο από console: `movie_list_generator.exe --cli`.

## Αρχεία που δημιουργούνται τοπικά (δεν ανεβαίνουν στο repo)

`config.json`, `library.json`, `movies_list.html`, `posters/`, `build/`, `dist/`, `release/`
