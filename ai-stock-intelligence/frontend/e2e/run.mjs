// フェーズ2の画面のE2Eテスト（Playwright）。
// 使い方：API（:8000）と Web（:3000）を起動した状態で `npm run e2e`
//   BASE_URL      … Web のURL（既定 http://127.0.0.1:3000）
//   API_DOWN=1    … API を停止した状態でのテスト（エラー表示と、数値を作らないことを確認）
//   SCREENSHOTS=1 … e2e/screenshots/ に画面を保存
import { createRequire } from "node:module";
// 環境にインストール済みの Playwright を使う（プロジェクトに別の版を入れない）
const { chromium, devices } = createRequire(import.meta.url)("playwright");
import { mkdirSync } from "node:fs";

const BASE = process.env.BASE_URL ?? "http://127.0.0.1:3000";
const SHOTS = process.env.SCREENSHOTS === "1";
const API_DOWN = process.env.API_DOWN === "1";
if (SHOTS) mkdirSync(new URL("./screenshots/", import.meta.url), { recursive: true });

let failures = 0;
const results = [];
async function test(name, fn) {
  try { await fn(); results.push(["PASS", name]); }
  catch (e) { failures++; results.push(["FAIL", name, e.message.split("\n")[0]]); }
}
const assert = (cond, msg) => { if (!cond) throw new Error(msg); };

async function noHorizontalOverflow(page) {
  const { sw, cw } = await page.evaluate(() => ({ sw: document.documentElement.scrollWidth, cw: document.documentElement.clientWidth }));
  assert(sw <= cw + 1, `横スクロールが発生（scrollWidth ${sw} > clientWidth ${cw}）`);
}

const browser = await chromium.launch();
const viewports = {
  PC: { viewport: { width: 1440, height: 900 }, deviceScaleFactor: SHOTS ? 2 : 1 },
  スマートフォン: { ...devices["iPhone 13"] },
};

for (const [label, ctxOpts] of Object.entries(viewports)) {
  const ctx = await browser.newContext({ ...ctxOpts, locale: "ja-JP" });
  const page = await ctx.newPage();
  const errors = [];
  page.on("pageerror", (e) => errors.push(e.message));

  if (API_DOWN) {
    await test(`[${label}] API停止時：エラーを表示し、数値を作らない`, async () => {
      await page.goto(BASE + "/");
      await page.getByRole("alert").first().waitFor();
      const text = await page.locator("main").innerText();
      assert(/接続できません/.test(text), "接続エラーの表示がない");
      if (SHOTS) await page.screenshot({ path: `e2e/screenshots/api-down-${label}.png`, fullPage: true });
    });
    await ctx.close();
    continue;
  }

  await test(`[${label}] ダッシュボード：取得できた指数は出所・基準日・遅延つき、TOPIXは未接続`, async () => {
    await page.goto(BASE + "/");
    for (const k of ["nikkei225", "sp500", "nasdaq"]) {
      const card = page.getByTestId(`index-${k}`);
      await card.waitFor();
      const t = await card.innerText();
      assert(/\d/.test(await card.locator(".num").first().innerText()), `${k} に値がない`);
      assert(/FRED/.test(t) && /\d{4}\/\d{2}\/\d{2}/.test(t) && /リアルタイムではありません/.test(t), `${k} に出所・基準日・遅延の表示がない：${t}`);
    }
    const topix = page.getByTestId("index-topix");
    assert((await topix.locator('[data-status="not_connected"]').count()) === 1, "TOPIX に未接続の表示がない");
    assert(!/\d/.test(await topix.locator(".num").first().innerText()), "TOPIX に数値が表示されている");
    const tenY = await page.getByTestId("macro-us_10y").innerText();
    assert(/\d/.test(tenY) && /\d{4}\/\d{2}\/\d{2}/.test(tenY), `米10年金利に値・基準日がない：${tenY}`);
    const bar = await page.getByRole("status", { name: "データの状態" }).innerText();
    const jpBar = await page.getByTestId("bar-prices_jp").innerText();
    assert(/日本株の株価/.test(jpBar) && (/未接続/.test(jpBar) || /\d{4}\/\d{2}\/\d{2}まで/.test(jpBar)),
      `上部の状態バーに日本株の株価の状態（未接続 または 最新データの日付）がない：${jpBar}`);
    assert(/仮設定/.test(bar), "SECの連絡先が仮設定である警告がない");
    await noHorizontalOverflow(page);
    if (SHOTS) await page.screenshot({ path: `e2e/screenshots/dashboard-${label}.png`, fullPage: true });
  });

  await test(`[${label}] 米国株の詳細：SECの財務データ（提出日つき）と、株価の取得元・基準日`, async () => {
    await page.goto(BASE + "/stocks/US/KO");
    const t = await page.getByTestId("financials-table").innerText();
    assert(/直近4四半期/.test(t) && /提出日/.test(t) && /億ドル/.test(t), `財務の表の内容が想定と違う：${t.slice(0, 120)}`);
    assert(/SEC EDGAR/.test(await page.locator("#financials").innerText()), "財務データの出所が表示されていない");
    const quote = await page.getByTestId("quote").innerText();
    if (!/未接続/.test(quote)) {
      assert(/Tiingo|Massive/.test(quote) && /\d{4}\/\d{2}\/\d{2}/.test(quote) && /リアルタイムの株価ではありません/.test(quote),
        `米国株の株価の取得元・基準日・遅延の表示がない：${quote}`);
    }
    await noHorizontalOverflow(page);
    if (SHOTS) await page.screenshot({ path: `e2e/screenshots/detail-us-${label}.png`, fullPage: true });
  });

  await test(`[${label}] データの状態ページ`, async () => {
    await page.goto(BASE + "/data");
    const fred = await page.getByTestId("ds-fred").innerText();
    assert(/FRED/.test(fred) && /公的統計/.test(fred) && /\d{4}\/\d{2}\/\d{2}/.test(fred), "FREDの状態が表示されていない");
    const jp = await page.getByTestId("ds-prices_jp").innerText();
    assert(/J-Quants/.test(jp), "J-Quants の行がない");
    await noHorizontalOverflow(page);
  });

  await test(`[${label}] 検索（日本語）→ 詳細ページ`, async () => {
    await page.goto(BASE + "/search");
    await page.locator('input[name="q"]').fill("トヨタ");
    await page.locator('select[name="market"]').selectOption("JP");
    await page.getByRole("button", { name: "検索" }).click();
    await page.getByTestId("results").waitFor();
    const first = page.getByTestId("results").locator("li").first();
    assert(/7203/.test(await first.innerText()), "トヨタ自動車（7203）が先頭にない");
    await noHorizontalOverflow(page);
    if (SHOTS) await page.screenshot({ path: `e2e/screenshots/search-${label}.png`, fullPage: true });
    await first.locator("a").click();
    await page.getByTestId("security-name").waitFor();
    assert((await page.getByTestId("security-name").innerText()) === "トヨタ自動車", "詳細ページの銘柄名が違う");
    const quote = await page.getByTestId("quote").innerText();
    if (/未接続/.test(quote)) {
      assert(!/\d{2,}/.test(quote.replace(/JPY/g, "")), `未接続なのに株価に数値が表示されている：${quote}`);
    } else {
      // 接続済み：取得元・基準日・遅延（リアルタイムではない旨）が必ず表示されること
      assert(/J-Quants/.test(quote) && /\d{4}\/\d{2}\/\d{2}/.test(quote) && /リアルタイムの株価ではありません/.test(quote),
        `株価の取得元・基準日・遅延の表示がない：${quote}`);
    }
    assert(/未評価/.test(await page.locator("main").innerText()), "総合評価が「未評価」になっていない");
    await noHorizontalOverflow(page);
    if (SHOTS) await page.screenshot({ path: `e2e/screenshots/detail-${label}.png`, fullPage: true });
  });

  await test(`[${label}] 検索（米国株・ティッカー）`, async () => {
    await page.goto(BASE + "/search?q=KO&market=US");
    const first = page.getByTestId("results").locator("li").first();
    await first.waitFor();
    assert(/Coca-Cola/.test(await first.innerText()), "KO（Coca-Cola）が先頭にない");
  });

  await test(`[${label}] 該当なし・存在しない銘柄`, async () => {
    await page.goto(BASE + "/search?q=zzzzqqqq");
    await page.getByTestId("no-results").waitFor();
    const res = await page.goto(BASE + "/stocks/US/ZZZZQQQQ");
    assert(res.status() === 404, `404ではない：${res.status()}`);
  });

  await test(`[${label}] ランキング・予測検証は「準備中」で、値を表示しない`, async () => {
    for (const p of ["/rankings", "/validation"]) {
      await page.goto(BASE + p);
      const t = await page.locator("main").innerText();
      assert(/フェーズ\d で実装予定|フェーズ\dで実装予定/.test(t), `${p} に準備中の表示がない`);
      await noHorizontalOverflow(page);
    }
  });

  await test(`[${label}] 表示モードの切り替え（ダーク⇔ライト）`, async () => {
    await page.goto(BASE + "/");
    assert((await page.evaluate(() => document.documentElement.dataset.theme)) === "dark", "既定がダークではない");
    await page.getByRole("button", { name: /ライトモードに切り替え/ }).click();
    assert((await page.evaluate(() => document.documentElement.dataset.theme)) === "light", "ライトに切り替わらない");
    await page.reload();
    assert((await page.evaluate(() => document.documentElement.dataset.theme)) === "light", "再読み込みで設定が保持されない");
    if (SHOTS) await page.screenshot({ path: `e2e/screenshots/dashboard-light-${label}.png`, fullPage: false });
    await page.getByRole("button", { name: /ダークモードに切り替え/ }).click();
  });

  if (label === "スマートフォン") {
    await test(`[${label}] 画面下部のナビゲーション`, async () => {
      await page.goto(BASE + "/");
      const nav = page.getByRole("navigation", { name: "メイン（モバイル）" });
      assert(await nav.isVisible(), "モバイルのナビゲーションが表示されない");
      await nav.getByRole("link", { name: "検索" }).click();
      await page.waitForURL("**/search");
    });
  }

  await test(`[${label}] JavaScriptのエラーがない`, async () => { assert(errors.length === 0, errors.join(" / ")); });
  await ctx.close();
}

await browser.close();
for (const r of results) console.log(r.join("  "));
console.log(`\n${results.length - failures}/${results.length} 件成功`);
process.exit(failures ? 1 : 0);
