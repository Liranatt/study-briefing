# מערכת הלימודים בשרת הביתי

העץ `מדעי המחשב` מועתק לשרת כ-`~/study/cs/`, באותו מבנה. הפרויקט עצמו הוא `~/study/cs/study-briefing`.

## מה רץ ומתי

כל לילה ב-01:30 UTC (03:30 בשעון ישראל), לפני ריצות הכספים ב-04:00 UTC, רץ `server/nightly.sh`. כל שלב רץ גם אם שלב קודם נכשל.

| שלב | מה | מתי |
|---|---|---|
| 1 | `moodle_pull.py --saved-token`, הקורסים של הסמסטר הנוכחי | תמיד |
| 2 | `forums_pull.py --saved-token`, אותם קורסים | תמיד |
| 3 | `file_new.py`: קבצים חדשים מהמודל נכנסים לעץ | תמיד |
| 4 | `catalog.py`, ואחריו `handwriting.py inject` | תמיד |
| 5 | `portal_pull.py`: מערכת שעות עם חדרים, מבחנים ומטלות, ציונים, הודעות | כשיש `secrets/bgu.env` |
| 6 | `mail_pull.py`: מייל מ-BGU, מהמודל ומהסגל, 14 ימים אחרונים, בלי קבצים מצורפים | כשיש `secrets/mail.env` |
| 7 | `calendar_pull.py`: האירועים הקרובים מיומן גוגל | כשיש `secrets/calendar.env` |
| 9 | `calendar_feed.py` ו-`site/build.py`: היומן לגוגל והאתר ב-`~/study/site` | תמיד |
| 8 | `recordings_nightly.py`, ואחריו `transcribe_cpu.py` עד 03:30 UTC | כשיש `secrets/bgu.env` |

ההקלטות רצות אחרונות, כי הן יכולות להימשך שעות, והאתר כבר מעודכן לפניהן. התמלול על המעבד (faster-whisper, המודל של ivrit-ai, int8, 8 ליבות) עוצר לפני 03:30 UTC, וקובץ שלא הסתיים ממשיך בלילה הבא.

**האתר:** הקונטיינר `study-web` (Caddy) על `127.0.0.1:8097` בלבד. הפורטים 8095 ו-8096 שייכים לאתרי הכספים. `/` הוא האתר, `/files/` הוא העץ לקריאה בלבד, ו-`/cal/<סוד>.ics` הוא היומן, בלי סיסמה כי גוגל לא יודע להתחבר. השם של הקובץ הוא הסוד.

## ההתקנה: פקודה אחת לכל דבר

כל סוד מוקלד בחלון מוסתר. שום דבר לא מודפס ולא נשמר מחוץ ל-`~/study/secrets`.

```bash
S=~/study/cs/study-briefing/server/setup.sh
$S bgu             # שם משתמש, סיסמה ות"ז; מקבל גם טוקן חדש למודל
$S site-password   # הסיסמה של האתר
$S tunnel          # טוקן של Cloudflare Tunnel; מראה איזה hostname להגדיר (study-web:80)
$S calendar        # הכתובת הסודית של יומן גוגל; מדפיס את כתובת היומן להרשמה
$S mail            # סיסמת אפליקציה של גוגל לתיבת BGU; מסביר איך יוצרים אותה
$S cron            # מוסיף את הריצה הלילית. שורות אחרות ב-crontab לא משתנות
$S status          # מה מוגדר, מה רץ לאחרונה
```

בדיקה של כל אוסף בלי לכתוב קבצים: `$S test portal`, `$S test mail`, `$S test calendar`, `$S test feed`, `$S test recordings`, `$S test transcribe`. ריצה לילית עכשיו: `$S run`.

## איפה מה

- לוגים: `~/study/logs/<תאריך>.log`. שורה שמתחילה ב-`!! step failed` היא שלב שנכשל. סיכום הריצה האחרונה ב-`~/study/logs/last_run.txt`.
- סודות: `~/study/secrets/` (תיקייה 700, קבצים 600). הסיסמה של האתר נשמרת רק כ-hash, ב-`secrets/web/site_auth`. זו התיקייה היחידה שהקונטיינר של האתר רואה.
- מה שהיה הולך לסל המחזור ב-Windows עובר כאן ל-`~/study/trash/<תאריך>/`.
- קבצי השרת: `~/study/server/` (docker-compose.yml ו-Caddyfile, מותקנים ב-`$S web`). שם הפרויקט ב-Docker הוא `study`, ואין לו קשר ל-fin-briefing או ל-fin-linoy.
- Python: `~/study/venv`. המודל לתמלול: `~/study/models/hf`.

## ההעתקה מהמחשב

`python -X utf8 sync_to_server.py` במחשב שולח רק קבצים חדשים או ששונו, ואפשר להריץ אותו שוב בכל זמן. עותק חדש יותר בשרת לא נדרס. קובץ שנמחק או זז במחשב עובר בשרת ל-`~/study/trash/sync-<תאריך>/`. התיקייה `קורסים`, `site\out` ו-`catalog\recordings_debug` לא מועתקות.

כדי שירוץ כל לילה לפני השרת (02:45 בשעון ישראל):

```
schtasks /Create /TN "study sync" /SC DAILY /ST 02:45 /TR "\"C:\Users\Liran\AppData\Local\Programs\Python\Python313\python.exe\" -X utf8 \"C:\Users\Liran\OneDrive\שולחן העבודה\מדעי המחשב\study-briefing\sync_to_server.py\""
```
