import urllib.request, json

url = "https://api.frankfurter.app/latest?from=EUR&to=GBP,USD"
with urllib.request.urlopen(url, timeout=10) as r:
      data = json.loads(r.read())

date = data["date"]
gbp = round(1 / data["rates"]["GBP"], 4)
usd = round(1 / data["rates"]["USD"], 4)

js_lines = [
      "// FX rates - written daily by GitHub Actions",
      "// EUR as base: how many EUR per 1 unit of foreign currency",
      "window.FX_RATES = {",
      "  GBP: " + str(gbp) + ",",
      "  USD: " + str(usd) + ",",
      '  date: "' + date + '",',
      '  source: "live"',
      "};"
]

with open("fx-rates.js", "w") as f:
      f.write("\n".join(js_lines) + "\n")

with open("fx-rates.json", "w") as f:
      json.dump({"GBP": gbp, "USD": usd, "date": date, "source": "live"}, f, indent=2)

print("GBP=" + str(gbp) + "  USD=" + str(usd) + "  date=" + date)
