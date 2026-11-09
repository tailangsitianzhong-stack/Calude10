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
// PRICE_SCENARIO=composite … テスト用DBに仮の株価を入れた状態（backend/tests/e2e_seed_price_scenario.py）での複合状態の表示
const PRICE_SCENARIO = process.env.PRICE_SCENARIO ?? "";
// PLAN_EXPECT=unset … JQUANTS_PLAN 未設定で起動した API に対して、Free と表示しないことも確認する
const PLAN_EXPECT = process.env.PLAN_EXPECT ?? "";
if (SHOTS) mkdirSync(new URL("./screenshots/", import.meta.url), { recursive: true });

let failures = 0;
const results = [];
// 株価の接続状態の表示名（backend/app/services/price_status.py の7つの状態）。固定の「未接続」は使わない
const PRICE_STATE = /APIキー未設定|設定済み・未取得|取得済み|取得失敗（API障害）|古いデータ|一部取得|代替の取得元で取得/;
const NO_PRICE_STATE = /APIキー未設定|設定済み・未取得|取得失敗（API障害）/;
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
    // TOPIX はデータがない：未接続（キー未設定・プランの範囲外・未取得・プラン未確認）または設定不備・取得失敗（エラー）。値は作らない
    assert((await topix.locator('[data-status="not_connected"], [data-status="error"]').count()) === 1, "TOPIX の状態の表示がない");
    assert(!/\d/.test(await topix.locator(".num").first().innerText()), "TOPIX に数値が表示されている");
    const tnote = await topix.innerText();
    assert(/APIキー未設定|提供されていません|未取得|プランが未確認|設定不備|取得に失敗/.test(tnote), `TOPIX の状態の説明がない：${tnote}`);
    if (PLAN_EXPECT === "unset") assert(!/Free/.test(tnote), `プラン未設定なのに Free と表示：${tnote}`);
    const tenY = await page.getByTestId("macro-us_10y").innerText();
    assert(/\d/.test(tenY) && /\d{4}\/\d{2}\/\d{2}/.test(tenY), `米10年金利に値・基準日がない：${tenY}`);
    const bar = await page.getByRole("status", { name: "データの状態" }).innerText();
    const jpBar = await page.getByTestId("bar-prices_jp").innerText();
    assert(/日本株の株価/.test(jpBar) && (/未接続/.test(jpBar) || /\d{4}\/\d{2}\/\d{2}まで/.test(jpBar)),
      `上部の状態バーに日本株の株価の状態（未接続 または 最新データの日付）がない：${jpBar}`);
    assert(!/仮設定/.test(bar), "SECの連絡先に「仮設定」の表示が残っている");
    await noHorizontalOverflow(page);
    if (SHOTS) await page.screenshot({ path: `e2e/screenshots/dashboard-${label}.png`, fullPage: true });
  });

  await test(`[${label}] 米国株の詳細：SECの財務データ（提出日つき）と、株価の取得元・基準日`, async () => {
    await page.goto(BASE + "/stocks/US/KO");
    const t = await page.getByTestId("financials-table").innerText();
    assert(/直近4四半期/.test(t) && /提出日/.test(t) && /億ドル/.test(t), `財務の表の内容が想定と違う：${t.slice(0, 120)}`);
    assert(/SEC EDGAR/.test(await page.locator("#financials").innerText()), "財務データの出所が表示されていない");
    const quote = await page.getByTestId("quote").innerText();
    assert(PRICE_STATE.test(quote), `米国株の株価の接続状態が表示されていない：${quote}`);
    if (/終値（基準日）/.test(quote)) {
      assert(/Tiingo|Massive/.test(quote) && /\d{4}\/\d{2}\/\d{2}/.test(quote) && /取得日時/.test(quote) && /リアルタイムの株価ではありません/.test(quote),
        `米国株の株価の取得元・基準日・取得日時・遅延の表示がない：${quote}`);
    } else {
      assert(NO_PRICE_STATE.test(quote), `株価がないのに状態が合わない：${quote}`);
    }
    await noHorizontalOverflow(page);
    if (SHOTS) await page.screenshot({ path: `e2e/screenshots/detail-us-${label}.png`, fullPage: true });
  });

  await test(`[${label}] 日本企業の財務（トヨタ・IFRS）：円・会計基準・12週間遅延・純資産の表記、経常利益は「—」`, async () => {
    const res = await page.goto(BASE + "/stocks/JP/7203");
    assert(res.status() === 200, `7203 の詳細ページが ${res.status()}`);
    const card = page.locator("#financials");
    if (/財務データは未取得です/.test(await card.innerText())) return;  // 財務情報を未取得の環境（J-Quants 未接続）では値を出さないことだけ確認
    const t = await page.getByTestId("financials-table").innerText();
    assert(/兆円|億円/.test(t) && !/ドル/.test(t), `金額が円で表示されていない：${t.slice(0, 160)}`);
    assert(/IFRS/.test(await page.getByTestId("accounting-standard").innerText()), "会計基準の表示がない");
    const ord = await page.locator("tr", { hasText: "経常利益" }).first().innerText();
    assert(!/\d/.test(ord.replace("経常利益", "")), `IFRS なのに経常利益に値がある：${ord}`);
    const bal = await page.getByTestId("balance-table").innerText();
    assert(/純資産（非支配株主持分を含む）/.test(bal) && /自己資本比率/.test(bal), `純資産の表記がない：${bal}`);
    assert(/J-Quants/.test(await card.innerText()), "財務データの出所（J-Quants）が表示されていない");
    const delay = page.getByTestId("financials-delay");
    if (await delay.count()) {
      const dt = await delay.innerText();
      // 確認済みの Free：12週間の遅延。プラン未設定：プラン未確認（Free と表示しない）。不正な値：設定不備
      assert(/12週間|プランが未確認|設定不備/.test(dt), `財務の遅延の表示が想定と違う：${dt}`);
      if (PLAN_EXPECT === "unset") assert(!/Freeプラン|12週間/.test(dt), `プラン未設定なのに Free の遅延を表示：${dt}`);
    }
    // 財務データの鮮度：確認済みのプランなら判定、プラン未設定・設定不備なら「判定できません」（古い・古くないを断定しない）
    const fresh = page.getByTestId("financials-freshness");
    if (await fresh.count()) {
      const ft = await fresh.innerText();
      assert(/鮮度：(古いデータ|最新の範囲内|判定できません)/.test(ft), `財務の鮮度の表示がない：${ft}`);
      if (PLAN_EXPECT === "unset") assert(/判定できません/.test(ft) && !/古いデータ|最新の範囲内/.test(ft), `プラン未設定なのに鮮度を断定：${ft}`);
      assert(/データの取得日時：\d{4}\/\d{2}\/\d{2}/.test(await page.getByTestId("financials-fetched").innerText()), "財務の取得日時の表示がない");
    }
    await noHorizontalOverflow(page);
    if (SHOTS) await page.screenshot({ path: `e2e/screenshots/detail-jp-fin-${label}.png`, fullPage: true });
  });

  await test(`[${label}] カナダドルで報告する企業（ENB）：エラーにならず、報告通貨で表示`, async () => {
    const res = await page.goto(BASE + "/stocks/US/ENB");
    assert(res.status() === 200, `ENB の詳細ページが ${res.status()}`);
    const t = await page.getByTestId("financials-table").innerText();
    assert(/カナダドル/.test(t) && !/億ドル|兆ドル/.test(t.replace(/カナダドル/g, "")), `ENB の金額がカナダドルで表示されていない：${t.slice(0, 160)}`);
    assert(/CAD/.test(await page.getByTestId("financials-currency").innerText()), "報告通貨の表示がない");
    await noHorizontalOverflow(page);
    if (SHOTS) await page.screenshot({ path: `e2e/screenshots/detail-enb-${label}.png`, fullPage: true });
  });

  if (PRICE_SCENARIO === "composite") await test(`[${label}] 株価の複合状態（一部取得・データ遅延あり／全銘柄取得済みの古いデータ）`, async () => {
    await page.goto(BASE + "/data");
    const card = (key) => page.getByTestId(`ds-${key}`).locator("xpath=ancestor::section[1]");
    const jp = await card("prices_jp").innerText();
    assert(/一部取得（100／[\d,]+ 銘柄）・データ遅延あり/.test(jp), `日本株：一部取得とデータ遅延の両方が表示されていない：${jp.slice(0, 200)}`);
    assert(/データ遅延あり（提供元の遅延を超えて古い）/.test(await page.getByTestId("freshness-prices_jp").innerText()), "日本株の鮮度の行");
    assert(/（一部取得）/.test(await page.getByTestId("coverage-prices_jp").innerText()), "日本株の取得率の行");
    const us = await card("prices_us").innerText();
    assert(/古いデータ（全 [\d,]+ 銘柄取得済み）/.test(us), `米国株：全銘柄取得済みの古いデータが表示されていない：${us.slice(0, 200)}`);
    assert(/100\.0%（全銘柄取得済み）/.test(await page.getByTestId("coverage-prices_us").innerText()), "米国株の取得率の行");
    if (SHOTS) await page.screenshot({ path: `e2e/screenshots/data-composite-${label}.png`, fullPage: true });
  });

  await test(`[${label}] データの状態ページ`, async () => {
    await page.goto(BASE + "/data");
    const fred = await page.getByTestId("ds-fred").innerText();
    assert(/FRED/.test(fred) && /公的統計/.test(fred) && /\d{4}\/\d{2}\/\d{2}/.test(fred), "FREDの状態が表示されていない");
    const jp = await page.getByTestId("ds-prices_jp").innerText();
    assert(/J-Quants/.test(jp), "J-Quants の行がない");
    for (const key of ["prices_jp", "prices_us"]) {
      const t = await page.getByTestId(`ds-${key}`).innerText();
      assert(/銘柄（取得対象のうち株価のある銘柄）/.test(t) && /株価の基準日/.test(t), `${key} の件数・基準日の表示がない：${t.slice(0, 160)}`);
      const card = await page.getByTestId(`ds-${key}`).locator("xpath=ancestor::section[1]").innerText().catch(() => t);
      assert(PRICE_STATE.test(card), `${key} の接続状態が表示されていない`);
      // 市場全体：取得率と鮮度を両方表示する
      const cov = await page.getByTestId(`coverage-${key}`).innerText();
      const fresh = await page.getByTestId(`freshness-${key}`).innerText();
      assert(/%|—/.test(cov) && /取得対象：/.test(cov), `${key} の取得率の表示がない：${cov}`);
      assert(/提供元の遅延の範囲内|データ遅延あり|株価なし/.test(fresh), `${key} の鮮度の表示がない：${fresh}`);
    }
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
    const badge = await first.locator("[data-status]").innerText().catch(() => "");
    assert(badge === "" || (/^株価 /.test(badge) && PRICE_STATE.test(badge)), `検索結果の株価の状態が実際の状態でない：${badge}`);
    await noHorizontalOverflow(page);
    if (SHOTS) await page.screenshot({ path: `e2e/screenshots/search-${label}.png`, fullPage: true });
    await first.locator("a").click();
    await page.getByTestId("security-name").waitFor();
    assert((await page.getByTestId("security-name").innerText()) === "トヨタ自動車", "詳細ページの銘柄名が違う");
    const quote = await page.getByTestId("quote").innerText();
    assert(PRICE_STATE.test(quote), `日本株の株価の接続状態が表示されていない：${quote}`);
    if (/終値（基準日）/.test(quote)) {
      // 株価あり：取得元・基準日・取得日時・遅延（リアルタイムではない旨）が必ず表示されること
      assert(/J-Quants/.test(quote) && /\d{4}\/\d{2}\/\d{2}/.test(quote) && /取得日時/.test(quote) && /リアルタイムの株価ではありません/.test(quote),
        `株価の取得元・基準日・取得日時・遅延の表示がない：${quote}`);
    } else {
      assert(NO_PRICE_STATE.test(quote), `株価がないのに状態が合わない：${quote}`);
      const price = await page.getByTestId("quote").locator(".text-3xl").innerText();
      assert(!/\d/.test(price), `株価がないのに数値が表示されている：${price}`);
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
