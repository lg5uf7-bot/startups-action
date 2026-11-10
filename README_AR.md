# Startup Companies + Jobs Collector

هذا المشروع يقوم بجمع الشركات الناشئة، وتوسيع بياناتها، والكشف عن نظام ATS الخاص بها، ثم الاحتفاظ بالوظائف النشطة في Supabase.

## ما الذي يفعله المشروع؟
- يكتشف الشركات الناشئة من مصادر مختلفة
- يضيف أو ي enrich معلومات الشركة
- يحدّد ATS الخاص بالشركة (مثل Greenhouse أو Lever أو Ashby)
- يتابع الوظائف المتاحة
- يحفظ البيانات بشكل آمن ومكرر (idempotent)
- ينفّذ المهام تلقائيًا عبر GitHub Actions

## التقنية المستخدمة
- Python 3.13
- GitHub Actions
- Supabase / PostgreSQL
- APIs مجانية فقط
- لا حاجة لخادم VPS

## البنية الأساسية للمشروع

```text
startups-action/
├── .github/
│   └── workflows/
│       └── daily-sync.yml
├── collector/
│   ├── __init__.py
│   ├── __main__.py
│   ├── config.py
│   ├── db.py
│   ├── http.py
│   ├── main.py
│   ├── models.py
│   ├── normalize.py
│   ├── scoring.py
│   ├── ats/
│   │   ├── __init__.py
│   │   ├── adapters.py
│   │   ├── detect.py
│   │   └── patterns.py
│   ├── pipeline/
│   │   ├── __init__.py
│   │   ├── discovery.py
│   │   ├── enrich.py
│   │   ├── jobs.py
│   │   ├── refresh.py
│   │   └── report.py
│   └── sources/
│       ├── __init__.py
│       ├── feeds.py
│       ├── hn.py
│       ├── remoteok.py
│       ├── remotive.py
│       ├── startupdb.py
│       ├── wwr.py
│       └── yc.py
├── .gitignore
├── requirements.txt
├── README.md
└── README_AR.md