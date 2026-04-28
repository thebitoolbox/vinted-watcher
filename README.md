# Vinted Watcher

Minimalny watcher Vinted uruchamiany przez GitHub Actions. Skrypt cyklicznie sprawdza zapisane wyszukiwanie, wykrywa nowe oferty i wysyła alert na Telegram. Harmonogram ustawiony jest co 15 minut i można go łatwo zmienić w `.github/workflows/vinted-watch.yml`.

## Struktura plików

```text
vinted-watcher/
├── .github/
│   └── workflows/
│       └── vinted-watch.yml
├── main.py
├── requirements.txt
├── seen_items.json
└── README.md
```

## Co musisz ustawić w GitHub Secrets

- `VINTED_SEARCH_URL` — pełny URL zapisanych wyników wyszukiwania na Vinted.
- `TELEGRAM_BOT_TOKEN` — token bota z BotFather.
- `TELEGRAM_CHAT_ID` — ID Twojego czatu lub grupy.

## Jak działa `seen_items.json`

Plik przechowuje listę już widzianych `item_id`. Przy pierwszym uruchomieniu skrypt zapisuje stan, ale nie wysyła alertu. Od drugiego uruchomienia wysyła tylko nowe oferty, a potem aktualizuje plik i workflow commitujący odsyła go do repo.

## Ważne uwagi

- Parser HTML w `extract_items()` jest celowo prosty i może wymagać dopasowania, jeśli Vinted zmieni strukturę strony.
- GitHub Actions uruchamia workflow w UTC. Jeśli chcesz częściej niż co 15 minut, minimalny cron dla `schedule` to co 5 minut.
- Jeśli nie chcesz publicznego repo, sprawdź limity minut dla prywatnych repo na swoim planie GitHub.
- Trzymaj tokeny wyłącznie w Secrets, nie w kodzie.

## Następne ulepszenia

- Dodać scoring okazji na podstawie ceny i słów kluczowych.
- Dodać wiele obserwowanych wyszukiwań z pliku konfiguracyjnego.
- Zmienić przechowywanie stanu z JSON na SQLite lub zewnętrzny store.
