# AI Financing Tracker (public)

Public debt, lease and structured financings to GPU clouds, AI campus developers and former
miners, scraped from [American Compute's GPU Financing Tracker](https://www.amcompute.com/gpu-financing-tracker)
with facility ratings from [ccir.io](https://ccir.io/credit#rated).

* `public/index.html` is the page GitHub Pages serves.
* `public/scrape.py` rebuilds `public/data/` and runs automatically every weekday morning
  (see `.github/workflows/refresh.yml`); press **Run workflow** under the Actions tab to refresh now.

The Market tab reads `public/data/market.enc.json`, an AES-256-GCM encrypted copy of the
Bloomberg prices and yields, and decrypts it in the browser with a password. Without the
password the data is unreadable. That file is pushed from the maintainer's machine, not built here.
