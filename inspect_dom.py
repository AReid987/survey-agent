#!/usr/bin/env python3
"""Minimal CDP DOM inspector — connects to ego-lite, dumps page state."""
import asyncio
import os

from playwright.async_api import async_playwright

LOG = os.path.expanduser("~/survey-agent/data/dom_inspect.log")
OUT_DIR = os.path.expanduser("~/survey-agent/data")
os.makedirs(OUT_DIR, exist_ok=True)

async def main():
    print("Connecting to CDP on 127.0.0.1:9222...", flush=True)
    try:
        async with async_playwright() as pw:
            browser = await pw.chromium.connect_over_cdp(
                "http://127.0.0.1:9222",
                timeout=60000  # Give it a full 60s
            )
            ctx = browser.contexts[0]
            print(f"Connected. Pages: {len(ctx.pages)}", flush=True)

            # Find topsurveys page
            target = None
            for pg in ctx.pages:
                url = pg.url
                print(f"  Page: {url}", flush=True)
                if "topsurveys" in url.lower():
                    target = pg
                    break

            if not target:
                # Check all pages
                print("No topsurveys page found — checking first page", flush=True)
                if ctx.pages:
                    target = ctx.pages[0]
                else:
                    print("FATAL: No pages at all", flush=True)
                    await browser.close()
                    return

            await target.bring_to_front()
            await asyncio.sleep(1)

            url = await target.evaluate("window.location.href")
            title = await target.evaluate("document.title")
            ready = await target.evaluate("document.readyState")
            print(f"\nURL: {url}", flush=True)
            print(f"TITLE: {title}", flush=True)
            print(f"READY: {ready}", flush=True)

            # Full page text
            text = await target.evaluate("document.body?.innerText || ''")
            print(f"\nPAGE TEXT ({len(text)} chars):")
            print("=" * 60)
            print(text[:4000])
            if len(text) > 4000:
                print(f"... ({len(text)-4000} more chars)")
            print("=" * 60)

            with open(os.path.join(OUT_DIR, "page_text.txt"), "w") as f:
                f.write(text)

            # Full HTML
            html = await target.evaluate("document.documentElement?.outerHTML || ''")
            with open(os.path.join(OUT_DIR, "dom_dump.html"), "w") as f:
                f.write(html)
            print(f"\nFull HTML saved: {len(html)} chars", flush=True)

            # ALL links
            links = await target.evaluate("""() => {
                return [...document.querySelectorAll('a')]
                    .filter(a => a.href)
                    .map(a => ({
                        href: a.href,
                        text: (a.innerText || '').trim().slice(0, 120),
                        cls: (a.className || '').slice(0, 60),
                        id: a.id || ''
                    }));
            }""")
            print(f"\nALL LINKS ({len(links)}):", flush=True)
            for l in links:
                if l['text'] or l['href'].includes('/survey/'):
                    print(f"  [{l['text'][:80]}] -> {l['href'][:100]}  class={l['cls']}", flush=True)

            # ALL form elements
            forms = await target.evaluate("""() => {
                return [...document.querySelectorAll('input, select, textarea, button, [role="button"], [contenteditable]')]
                    .map(el => ({
                        tag: el.tagName.toLowerCase(),
                        type: el.type || '',
                        name: el.name || '',
                        id: el.id || '',
                        cls: (el.className || '').slice(0, 60),
                        text: (el.innerText || el.value || el.title || '').trim().slice(0, 60),
                        visible: el.offsetParent !== null,
                        href: el.href || ''
                    }));
            }""")
            print(f"\nFORM ELEMENTS ({len(forms)}):", flush=True)
            for f in forms:
                if f['visible']:
                    print(f"  <{f['tag']} type={f['type']} name={f['name']} id={f['id']} class={f['cls']}> '{f['text'][:50]}'", flush=True)

            # Search for $ amounts (balance)
            dollars = await target.evaluate("""() => {
                const matches = [];
                const els = document.querySelectorAll('*');
                for (const el of els) {
                    if (el.children.length > 0) continue;  // Only leaf nodes
                    const t = el.textContent?.trim();
                    if (t && /\\$[0-9]+\\.[0-9][0-9]/.test(t) && t.length < 30) {
                        matches.push({tag: el.tagName, text: t, id: el.id, cls: (el.className||'').slice(0,60)});
                    }
                }
                return matches;
            }""")
            print(f"\nDOLLAR AMOUNTS ({len(dollars)}):", flush=True)
            for d in dollars:
                print(f"  <{d['tag']}> '{d['text']}' id={d['id']} class={d['cls']}", flush=True)

            # Balance-specific search
            balance_info = await target.evaluate("""() => {
                // Search many patterns for balance
                const sels = [
                    '#balance', '.balance', '[class*=balance]', '[class*=earning]',
                    '[class*=credit]', '[class*=point]', '[class*=money]',
                    '#points', '.points', '.amount', '.reward',
                    'nav span', 'header span', '[class*=user]',
                    '[class*=account]', '[class*=wallet]', '[class*=total]',
                    '.dollar', '.currency', '[class*=coins]'
                ];
                const results = {};
                for (const sel of sels) {
                    const el = document.querySelector(sel);
                    if (el) results[sel] = el.outerHTML.slice(0, 300);
                }
                return results;
            }""")
            print("\nBALANCE SELECTORS:", flush=True)
            for k, v in balance_info.items():
                print(f"  {k}: {v[:200]}", flush=True)

            # Check localStorage for auth/user data
            ls = await target.evaluate("""() => {
                const data = {};
                for (let i = 0; i < localStorage.length; i++) {
                    const k = localStorage.key(i);
                    const v = localStorage.getItem(k);
                    data[k] = (v || '').slice(0, 200);
                }
                return data;
            }""")
            print(f"\nLOCALSTORAGE ({len(ls)} items):", flush=True)
            for k, v in ls.items():
                relevant = any(x in k.lower() for x in ['auth', 'token', 'user', 'session', 'balance', 'credit', 'email', 'id', 'profile'])
                if relevant:
                    print(f"  {k}: {v[:100]}", flush=True)

            # Check cookies
            cookies = await target.evaluate("""() => {
                return document.cookie;
            }""")
            print(f"\nCOOKIES: {cookies[:500] if cookies else 'none'}", flush=True)

            # Check for iframes
            iframes = await target.evaluate("""() => {
                return [...document.querySelectorAll('iframe')].map(f => ({
                    src: f.src || '',
                    id: f.id || '',
                    cls: (f.className || '').slice(0, 40),
                }));
            }""")
            print(f"\nIFRAMES ({len(iframes)}):", flush=True)
            for fr in iframes:
                print(f"  src={fr['src'][:100]} id={fr['id']}", flush=True)

            await browser.close()
            print("\n=== DONE ===", flush=True)

    except Exception as e:
        print(f"ERROR: {e}", flush=True)
        import traceback
        traceback.print_exc()

asyncio.run(main())
