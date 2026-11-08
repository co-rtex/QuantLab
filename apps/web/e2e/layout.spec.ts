import { expect, test, type Page } from "@playwright/test";

// No page scrolls sideways, at a desktop or a phone width (audit 2026-11-07, A6/A8/A14/A24):
// wide tables scroll inside their own card instead. Before, the compare statistics widened
// the page to 1,346 px at 1,200, and the experiment page was 851 px wide at 390.

/** The outermost elements that cross the right edge outside any scroll container. */
async function overflow(page: Page): Promise<{ scroll: number; client: number; culprits: string[] }> {
  return page.evaluate(() => {
    const vw = document.documentElement.clientWidth;
    const scrolls = (e: Element) => ["auto", "scroll"].includes(getComputedStyle(e).overflowX);
    const culprits: string[] = [];
    for (const e of Array.from(document.querySelectorAll("body *"))) {
      if (e.getBoundingClientRect().right <= vw + 1) continue;
      let p = e.parentElement;
      let contained = false;
      let parentOver = false;
      while (p && p !== document.body) {
        if (scrolls(p)) contained = true;
        if (p === e.parentElement && p.getBoundingClientRect().right > vw + 1) parentOver = true;
        p = p.parentElement;
      }
      if (!contained && !parentOver) {
        const cls = typeof e.className === "string" ? e.className.slice(0, 60) : "";
        culprits.push(`${e.tagName.toLowerCase()}.${cls} right=${Math.round(e.getBoundingClientRect().right)}`);
      }
    }
    // Text can run past its own box without widening it (a long unbroken word): name the
    // innermost elements whose content overflows them, outside any scroll container.
    if (!culprits.length) {
      for (const e of Array.from(document.querySelectorAll("main *"))) {
        if (e.scrollWidth <= e.clientWidth + 1 || scrolls(e) || e.querySelector("*:not(br)")) continue;
        if (getComputedStyle(e).position === "absolute" && e.clientWidth <= 1) continue; // sr-only
        let p = e.parentElement;
        while (p && !scrolls(p)) p = p.parentElement;
        if (!p || p === document.documentElement) culprits.push(`text in ${e.tagName.toLowerCase()}: ${(e.textContent ?? "").slice(0, 40)}`);
      }
    }
    return { scroll: document.documentElement.scrollWidth, client: vw, culprits: culprits.slice(0, 8) };
  });
}

test("no page scrolls sideways at 1280 or 390 px", async ({ page }) => {
  test.setTimeout(180_000);
  await page.goto("/register");
  await page.getByLabel("Name").fill("Layout");
  await page.getByLabel("Email").fill(`layout-${Date.now()}@example.com`);
  await page.getByLabel("Password").fill("Layout-check-2026");
  await page.getByRole("button", { name: "Create account" }).click();
  await expect(page.getByText("This workspace has no market data yet")).toBeVisible();

  const ws = (await (await page.request.get("/api/v1/workspaces")).json())[0].id;
  const job = await (
    await page.request.post(`/api/v1/workspaces/${ws}/datasets/synthetic`, {
      data: { slug: `layout-${Date.now()}`, name: "Layout data", n_assets: 12, start: "2020-01-02", end: "2022-12-30" },
    })
  ).json();
  await expect.poll(async () => (await (await page.request.get(`/api/v1/jobs/${job.id}`)).json()).status, { timeout: 30_000 }).toBe("completed");
  const dv = (await (await page.request.get(`/api/v1/workspaces/${ws}/dataset-versions`)).json())[0].id;

  const ids: string[] = [];
  for (const strategy_key of ["equal_weight", "momentum"]) {
    const sub = await (
      await page.request.post(`/api/v1/workspaces/${ws}/experiments`, {
        data: { name: `Layout ${strategy_key}`, dataset_version_id: dv, strategy_key, config: { start: "2021-01-04", benchmark: "SYNIDX" } },
      })
    ).json();
    ids.push(sub.experiment.id);
  }
  for (const id of ids) {
    await expect.poll(async () => (await (await page.request.get(`/api/v1/experiments/${id}`)).json()).status, { timeout: 60_000 }).toBe("completed");
  }
  const pf = await (
    await page.request.post(`/api/v1/workspaces/${ws}/portfolios`, {
      data: { name: "Layout book", dataset_version_id: dv, initial_deposit: 100_000, start_date: "2020-01-02" },
    })
  ).json();

  const pages: [string, (p: Page) => Promise<void>][] = [
    ["/", async () => {}],
    ["/data", async () => {}],
    [`/data/${dv}`, async (p) => expect(p.getByText("Quality checks")).toBeVisible()],
    ["/experiments", async () => {}],
    ["/experiments/new", async () => {}],
    [`/experiments/${ids[1]}`, async (p) => expect(p.getByRole("heading", { name: "Largest drawdowns" })).toBeVisible({ timeout: 30_000 })],
    [
      "/compare",
      async (p) => {
        for (const box of await p.getByRole("checkbox").all()) await box.check();
        await p.getByRole("button", { name: "Compare selected" }).click();
        await expect(p.getByRole("heading", { name: "Statistics" })).toBeVisible({ timeout: 30_000 });
      },
    ],
    ["/portfolios", async () => {}],
    [`/portfolios/${pf.id}`, async (p) => expect(p.getByRole("heading", { name: "Ledger" })).toBeVisible({ timeout: 30_000 })],
    ["/optimize", async () => {}],
    ["/ml", async () => {}],
    ["/settings", async (p) => expect(p.getByRole("heading", { name: "Audit log" })).toBeVisible()],
  ];
  const failures: string[] = [];
  for (const width of [1280, 390]) {
    await page.setViewportSize({ width, height: 900 });
    for (const [path, ready] of pages) {
      await page.goto(path);
      await page.waitForLoadState("networkidle");
      await ready(page);
      const o = await overflow(page);
      if (o.scroll > o.client + 1) failures.push(`${width}px ${path}: page ${o.scroll} > ${o.client}; ${o.culprits.join(" | ")}`);
    }
  }
  expect(failures, failures.join("\n")).toEqual([]);
});
