# AI Financing Tracker (public)

Public debt, lease and structured financings to GPU clouds, AI campus developers and former
miners, scraped from [American Compute's GPU Financing Tracker](https://www.amcompute.com/gpu-financing-tracker)
with facility ratings from [ccir.io](https://ccir.io/credit#rated).

* `index.html` is the page GitHub Pages serves.
* `scrape.py` rebuilds `data/` and runs automatically every weekday morning
  (see `.github/workflows/refresh.yml`); press **Run workflow** under the Actions tab to refresh now.

No market prices or yields are published here.
