// API route Vercel : POST /api/trigger-scrape
//
// Déclenche à distance le workflow GitHub Actions "scrape.yml" (celui
// qui lance scrape_all.py et committe data/lots.json), sans avoir à
// aller sur GitHub.
//
// Nécessite 3 variables d'environnement sur Vercel (Project Settings
// -> Environment Variables) :
//   GITHUB_TOKEN  -> Personal Access Token GitHub avec la permission
//                     "Actions: Read and write" sur ce repo
//   GITHUB_OWNER  -> ton pseudo/organisation GitHub (ex: "sergio123")
//   GITHUB_REPO   -> le nom du repo (ex: "Ddr")
//
// Le workflow doit s'appeler "scrape.yml" (chemin
// .github/workflows/scrape.yml) et être sur la branche "main".

export default async function handler(req, res) {
  if (req.method !== "POST") {
    res.status(405).json({ error: "Méthode non autorisée, utilise POST." });
    return;
  }

  const { GITHUB_TOKEN, GITHUB_OWNER, GITHUB_REPO } = process.env;

  if (!GITHUB_TOKEN || !GITHUB_OWNER || !GITHUB_REPO) {
    res.status(500).json({
      error:
        "Configuration manquante côté serveur : GITHUB_TOKEN, GITHUB_OWNER ou GITHUB_REPO n'est pas défini sur Vercel.",
    });
    return;
  }

  const url = `https://api.github.com/repos/${GITHUB_OWNER}/${GITHUB_REPO}/actions/workflows/scrape.yml/dispatches`;

  try {
    const ghResponse = await fetch(url, {
      method: "POST",
      headers: {
        Authorization: `Bearer ${GITHUB_TOKEN}`,
        Accept: "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "Content-Type": "application/json",
      },
      body: JSON.stringify({ ref: "main" }),
    });

    if (ghResponse.status === 204) {
      res.status(200).json({ ok: true, message: "Scraping déclenché." });
      return;
    }

    const detail = await ghResponse.text();
    res.status(502).json({
      error: `GitHub a répondu ${ghResponse.status}.`,
      detail,
    });
  } catch (err) {
    res.status(500).json({ error: "Échec de l'appel à l'API GitHub.", detail: String(err) });
  }
}
