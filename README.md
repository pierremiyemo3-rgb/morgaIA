# MorgaIA — Plateforme d'Analyse Football IA

## Structure des fichiers

```
ScoutIA/
├── api/
│   └── index.py        ← Backend Flask (Vercel)
├── index.html          ← Frontend
├── database.py         ← Base SQLite (local)
├── requirements.txt    ← Dépendances Python
├── vercel.json         ← Config Vercel
└── .gitignore
```

## Déploiement GitHub + Vercel

### 1. GitHub
1. Crée un repo sur github.com
2. Upload tous les fichiers
3. Commit et push

### 2. Vercel
1. Va sur vercel.com → "New Project"
2. Importe ton repo GitHub
3. Dans "Environment Variables", ajoute :
   - `FOOTBALL_KEY` = ta clé API Football
   - `VERCEL` = `1`
4. Clique "Deploy"

## Lancement local
```bash
python server.py
```
Puis ouvre http://localhost:8765
