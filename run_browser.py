#!/usr/bin/env python3
"""
Cloud browser runner (for Koyeb / any Docker host).

Keeps ONE real Chromium tab open on arena.ai (admin logs in once via the
noVNC browser view). Student questions arrive through bridge_server.py
(/bridge/poll); this runner types each question into the Arena tab, waits
until generation fully finishes, reads ONLY the new answer from the page,
and posts it back (/bridge/result). Arena stays invisible to students.
"""
import json, os, time, urllib.request, urllib.error

BRIDGE = os.environ.get("BRIDGE", "http://127.0.0.1:9080").rstrip("/")
BRIDGE_KEY = os.environ.get("BRIDGE_KEY", "123456")
PROFILE = os.environ.get("PROFILE_DIR", "/data/profile")
ARENA_URL = "https://arena.ai/"

def log(msg):
    print(time.strftime("%H:%M:%S ") + str(msg), flush=True)

def bridge_post(path, payload, timeout=40):
    req = urllib.request.Request(
        BRIDGE + path,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", "X-Bridge-Key": BRIDGE_KEY},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read() or b"{}")

SNAPSHOT_JS = r"""() => {
  const els = [...document.querySelectorAll('main div, main article, main section, main li, [class*="message"] div')];
  const texts = [];
  for (const e of els) {
    const t = (e.innerText || '').trim();
    if (t.length < 30) continue;
    const childBig = [...e.children].some(c => ((c.innerText || '').trim().length > t.length * 0.85));
    if (!childBig) texts.push(t);
  }
  return { texts, full: (document.querySelector('main') || document.body).innerText || '' };
}"""

READ_JS = r"""({ before, prompt }) => {
  const norm = s => (s || '').replace(/\s+/g, ' ').trim();
  const isJunk = t => /third-party AI|responses may be inaccurate/i.test(t) ||
    /^(New chat|Direct|Battle|Leaderboard|History)$/i.test(t.trim());
  const generating = (() => {
    if (document.querySelector('button[aria-label*="Stop" i]')) return true;
    const m = document.querySelector('main');
    const t = m ? (m.innerText || '') : '';
    return /Generating\.\.\.|Generating…/.test(t.slice(-3000));
  })();
  const els = [...document.querySelectorAll('main div, main article, main section, main li, [class*="message"] div')];
  const bs = [];
  for (const e of els) {
    const t = (e.innerText || '').trim();
    if (t.length < 30) continue;
    const childBig = [...e.children].some(c => ((c.innerText || '').trim().length > t.length * 0.85));
    if (!childBig) bs.push(t);
  }
  let startIdx = -1;
  bs.forEach((t, i) => { if (norm(t) === norm(prompt)) startIdx = i; });
  const beforeSet = new Set(before.texts);
  const bf = norm(before.full);
  const cands = bs.filter((t, i) =>
    (startIdx < 0 || i > startIdx) && !beforeSet.has(t) && !isJunk(t) && !bf.includes(norm(t)));
  let answer = '';
  if (cands.length) {
    cands.sort((a, b) => b.length - a.length);
    answer = cands[0];
    if (norm(answer).startsWith(norm(prompt))) answer = answer.slice(prompt.length).trim();
    if (answer.length <= 25) answer = '';
  }
  return { generating, answer };
}"""

def ensure_page(ctx, page):
    try:
        if page is None or page.is_closed():
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
    except Exception:
        page = ctx.new_page()
    try:
        if "arena.ai" not in (page.url or ""):
            page.goto(ARENA_URL, wait_until="domcontentloaded", timeout=60000)
    except Exception as e:
        log(f"goto arena failed: {e}")
    return page

def handle_job(page, job):
    rid, prompt = job["requestId"], job["prompt"]
    log(f"job {rid}: {prompt[:80]!r}")
    page.wait_for_selector("textarea", timeout=20000)
    # Wait for any previous generation to finish before typing
    t0 = time.time()
    while time.time() - t0 < 60:
        st = page.evaluate(READ_JS, {"before": {"texts": [], "full": ""}, "prompt": "\u0001"})
        if not st["generating"]:
            break
        time.sleep(1)
    before = page.evaluate(SNAPSHOT_JS)
    box = page.locator("textarea").first
    box.click()
    page.keyboard.press("Control+A")
    page.keyboard.press("Backspace")
    page.keyboard.type(prompt, delay=8)
    try:
        page.wait_for_function(
            """() => { const b = document.querySelector('button[aria-label="Send message"]'); return b && !b.disabled; }""",
            timeout=10000,
        )
        page.click('button[aria-label="Send message"]')
    except Exception:
        page.keyboard.press("Enter")
        time.sleep(2)
        val = box.input_value()
        if val.strip():
            raise RuntimeError("Send button not ready (chat box empty/blocked?).")
    last, stable, answer = "", 0, ""
    t0 = time.time()
    while time.time() - t0 < 180:
        time.sleep(1)
        st = page.evaluate(READ_JS, {"before": before, "prompt": prompt})
        cur = st["answer"] or ""
        if cur != last:
            log(f"  generating={st['generating']} read {len(cur)} chars")
        if not st["generating"] and cur and len(cur) > 25 and cur == last:
            stable += 1
            if stable >= 3:
                answer = cur
                break
        else:
            stable = 0
        last = cur
    if not answer:
        st = page.evaluate(READ_JS, {"before": before, "prompt": prompt})
        if st["answer"] and len(st["answer"]) > 25:
            answer = st["answer"]
    if not answer:
        dump = ""
        try:
            dump = page.evaluate("() => ((document.querySelector('main') || document.body).innerHTML || '').slice(0, 15000)")
        except Exception:
            pass
        bridge_post("/bridge/result", {"requestId": rid, "ok": False,
                                       "error": "No answer could be read from Arena page.",
                                       "rawSample": "DOM-DUMP: " + dump}, timeout=15)
        log(f"job {rid}: FAILED (no answer read)")
        return
    bridge_post("/bridge/result", {"requestId": rid, "ok": True, "answer": answer}, timeout=15)
    log(f"job {rid}: answered ({len(answer)} chars)")

def main():
    from playwright.sync_api import sync_playwright
    os.makedirs(PROFILE, exist_ok=True)
    while True:
        try:
            with sync_playwright() as pw:
                ctx = pw.chromium.launch_persistent_context(
                    PROFILE,
                    headless=False,
                    viewport={"width": 1280, "height": 900},
                    user_agent="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
                    args=["--no-sandbox", "--disable-dev-shm-usage",
                          "--disable-blink-features=AutomationControlled", "--mute-audio"],
                    ignore_default_args=["--enable-automation"],
                )
                ctx.set_default_timeout(30000)
                page = ensure_page(ctx, None)
                log("Browser up. If Arena shows a login page, log in via the noVNC browser view, choose Direct + your model, then leave the tab open.")
                while True:
                    try:
                        job = bridge_post("/bridge/poll", {}, timeout=35)
                    except urllib.error.URLError:
                        time.sleep(3)
                        continue
                    if job.get("requestId"):
                        page = ensure_page(ctx, page)
                        try:
                            handle_job(page, job)
                        except Exception as e:
                            log(f"job {job['requestId']} error: {e}")
                            try:
                                page.screenshot(path="/tmp/last_error.png")
                            except Exception:
                                pass
                            try:
                                bridge_post("/bridge/result", {"requestId": job["requestId"], "ok": False,
                                                               "error": f"Arena tab error: {e}"}, timeout=15)
                            except Exception:
                                pass
        except Exception as e:
            log(f"browser crashed, restarting in 5s: {e}")
            time.sleep(5)

if __name__ == "__main__":
    main()
