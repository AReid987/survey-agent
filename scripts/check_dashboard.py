"""Check what URLs are on the Branded Surveys dashboard."""
import json
import pathlib
from playwright.sync_api import sync_playwright
from cloakbrowser import browser as _cb_browser

profile_dir = pathlib.Path("/Users/antonioreid/survey-agent/data/cloak_profile")
binary_path = _cb_browser.ensure_binary()

pw = sync_playwright().start()
ctx = pw.chromium.launch_persistent_context(
    user_data_dir=str(profile_dir),
    executable_path=binary_path,
    headless=False,
    args=["--fingerprint-timezone=America/New_York", "--lang=en-US"],
)
page = ctx.pages[0]
page.goto("https://surveys.gobranded.com/users/dashboard", wait_until="domcontentloaded", timeout=30000)
page.wait_for_timeout(8000)

url = page.url
text = page.evaluate("document.body ? document.body.innerText.substring(0, 500) : ''")
print(f"URL: {url}")
print(f"Text preview: {text[:300]!r}")
print()

result = page.evaluate("""() => {
    const allLinks = document.querySelectorAll('a[href]');
    const list = [];
    allLinks.forEach(el => {
        const href = el.href || '';
        const text = (el.textContent || '').trim();
        if (text || href) list.push({text: text.substring(0, 50), href: href.substring(0, 200)});
    });
    return list;
}""")
print(f"Total links: {len(result)}")

survey_links = [r for r in result if "/surveys/pre" in r.get("href", "") or "/surveys/take" in r.get("href", "")]
print(f"Survey-looking links: {len(survey_links)}")
for r in survey_links[:10]:
    print(f"  {r}")

print()
print("All unique hrefs containing 'surveys':")
hrefs = sorted(set(r.get("href", "") for r in result if "surveys" in r.get("href", "")))
for h in hrefs[:30]:
    print(f"  {h}")

ctx.close()
pw.stop()