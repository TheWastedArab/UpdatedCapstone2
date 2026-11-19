/* Automated accessibility check of the webview (NFR-12), through the browser harness.

     cd backend && python -m scripts.webview_harness           (demo data from scripts.seed)
     cd extension && npm install --no-save playwright @axe-core/playwright && npx playwright install chromium
     node scripts/a11y-audit.js http://127.0.0.1:8000

   For every screen it runs axe-core with the WCAG 2.1 A and AA rules, under three
   colour themes: the harness defaults (light) and the colour values of VS Code's
   Dark Modern and High Contrast themes, applied as the same CSS variables VS Code
   sets in a real webview. It then presses Tab through the bug list and checks that
   every interactive element is reached and shows a visible focus indicator.
   Writes docs/evaluation/accessibility.json.

   Automated rules find roughly a third to a half of WCAG problems; this does not
   replace testing with a screen reader. */
const fs = require('fs');
const path = require('path');
const { chromium } = require('playwright');
const { AxeBuilder } = require('@axe-core/playwright');

const base = (process.argv[2] || 'http://127.0.0.1:8000').replace(/\/$/, '');
const PASSWORD = 'Demo!Pass123'; // demo seed account, development database only
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

const THEMES = {
  light: {},
  'dark modern': {
    'editor-background': '#1f1f1f', foreground: '#cccccc', descriptionForeground: '#9d9d9d', 'sideBar-background': '#181818',
    'panel-border': '#2b2b2b', 'input-background': '#313131', 'input-border': '#3c3c3c', 'input-foreground': '#cccccc',
    'button-background': '#0078d4', 'button-foreground': '#ffffff', 'button-hoverBackground': '#026ec1',
    'button-secondaryBackground': '#313131', 'button-secondaryForeground': '#cccccc', 'badge-background': '#616161',
    'badge-foreground': '#f8f8f8', focusBorder: '#0078d4', 'textLink-foreground': '#4daafc', errorForeground: '#f85149',
    'charts-blue': '#3794ff', 'charts-green': '#89d185', 'inputValidation-infoBackground': '#063b49',
    'inputValidation-infoBorder': '#007acc', 'inputValidation-warningBackground': '#352a05', 'inputValidation-warningBorder': '#b89500',
  },
  'high contrast': {
    'editor-background': '#000000', foreground: '#ffffff', descriptionForeground: '#ffffff', 'sideBar-background': '#000000',
    'panel-border': '#6fc3df', 'input-background': '#000000', 'input-border': '#6fc3df', 'input-foreground': '#ffffff',
    'button-background': '#000000', 'button-foreground': '#ffffff', 'button-hoverBackground': '#000000',
    'button-secondaryBackground': '#000000', 'button-secondaryForeground': '#ffffff', 'badge-background': '#000000',
    'badge-foreground': '#ffffff', focusBorder: '#f38518', 'textLink-foreground': '#21a6ff', errorForeground: '#f48771',
    'charts-blue': '#3794ff', 'charts-green': '#89d185', 'inputValidation-infoBackground': '#000000',
    'inputValidation-infoBorder': '#6fc3df', 'inputValidation-warningBackground': '#000000', 'inputValidation-warningBorder': '#6fc3df',
  },
};
const SCREENS = [['bug list', { view: 'bugs' }], ['report form', { view: 'report' }], ['dashboard', { view: 'dashboard' }],
  ['notifications', { view: 'notifications' }], ['projects and team', { view: 'team' }], ['administration', { view: 'admin' }]];

async function axe(page) {
  const r = await new AxeBuilder({ page }).withTags(['wcag2a', 'wcag2aa', 'wcag21a', 'wcag21aa']).analyze();
  return { rules_passed: r.passes.length, needs_review: r.incomplete.map((v) => v.id),
    violations: r.violations.map((v) => ({ rule: v.id, impact: v.impact, help: v.help, elements: v.nodes.length, first: v.nodes[0].target.join(' ') })) };
}

async function navigate(page, route) {
  await page.evaluate((r) => window.postMessage({ type: 'navigate', route: r }, '*'), route);
  await sleep(1200);
}

async function keyboard(page) {
  await navigate(page, { view: 'bugs' });
  const total = await page.evaluate(() => {
    const items = [...document.querySelectorAll('button, a[href], input, select, textarea, [tabindex]:not([tabindex="-1"])')]
      .filter((e) => !e.disabled && e.offsetParent !== null);
    items.forEach((e, i) => { e.dataset.a11yKey = String(i); });
    return items.length;
  });
  const reached = new Map();
  for (let i = 0; i < total * 3; i++) {
    await page.keyboard.press('Tab');
    const [key, visible] = await page.evaluate(() => {
      const e = document.activeElement;
      const s = getComputedStyle(e);
      return [e.dataset.a11yKey, (s.outlineStyle !== 'none' && parseFloat(s.outlineWidth) > 0) || s.boxShadow !== 'none'];
    });
    if (key !== undefined) reached.set(key, (reached.get(key) || true) && visible);
  }
  return { interactive_elements: total, reached_by_tab: reached.size, without_visible_focus: [...reached.values()].filter((v) => !v).length };
}

(async () => {
  const browser = await chromium.launch();
  const report = { checked_at: new Date().toISOString(), standard: 'WCAG 2.1 A and AA rules of axe-core', themes: {} };
  for (const [theme, colours] of Object.entries(THEMES)) {
    const context = await browser.newContext();
    const page = await context.newPage();
    await page.goto(base + '/harness');
    await page.evaluate((c) => { for (const [k, v] of Object.entries(c)) document.documentElement.style.setProperty('--vscode-' + k, v); }, colours);
    await sleep(800);
    const screens = { 'sign in': await axe(page) };
    await page.getByLabel('Email address').fill('admin@demo.example.com');
    await page.getByLabel('Password').fill(PASSWORD);
    await page.getByRole('button', { name: 'Sign in' }).click();
    await sleep(2000);
    for (const [name, route] of SCREENS) { await navigate(page, route); screens[name] = await axe(page); }
    await navigate(page, { view: 'bugs' });
    await page.locator('table button').first().click();
    await sleep(1500);
    screens['bug detail'] = await axe(page);
    report.themes[theme] = { screens, keyboard: await keyboard(page) };
    await context.close();
  }
  await browser.close();
  const out = path.resolve(__dirname, '..', '..', 'docs', 'evaluation', 'accessibility.json');
  fs.writeFileSync(out, JSON.stringify(report, null, 2));
  for (const [theme, t] of Object.entries(report.themes)) {
    for (const [name, s] of Object.entries(t.screens)) {
      console.log(`${theme.padEnd(14)} ${name.padEnd(18)} ${s.violations.length} violations, ${s.rules_passed} rules passed` +
        s.violations.map((v) => `\n    [${v.impact}] ${v.rule} x${v.elements}: ${v.help} (${v.first})`).join(''));
    }
    const k = t.keyboard;
    console.log(`${theme.padEnd(14)} keyboard           ${k.reached_by_tab}/${k.interactive_elements} reached by Tab, ${k.without_visible_focus} without visible focus`);
  }
  console.log('Written to ' + out);
})();
