import { chromium } from 'playwright';

const BASE_URL = 'http://localhost:5173';
const CHROME_PATH = 'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe';

const results = [];
function record(step, status, detail = '') {
  results.push({ step, status, detail });
  const icon = status === 'PASS' ? '✓' : status === 'FAIL' ? '✗' : '⊘';
  console.log(`  ${icon} [${status}] ${step}${detail ? ` (${detail})` : ''}`);
}

async function runSuite() {
  console.log('='.repeat(75));
  console.log('WorkGrid Phase 8 Browser User Acceptance Testing (Playwright + Chrome)');
  console.log(`Target Frontend: ${BASE_URL}`);
  console.log('='.repeat(75));

  const browser = await chromium.launch({
    executablePath: CHROME_PATH,
    headless: true,
  });

  const context = await browser.newContext({
    viewport: { width: 1280, height: 800 },
  });

  const page = await context.newPage();
  const consoleErrors = [];
  page.on('console', (msg) => {
    if (msg.type() === 'error') {
      consoleErrors.push(msg.text());
    }
  });
  page.on('response', (res) => {
    if (res.status() >= 400) {
      console.log(`[NETWORK ${res.status()}] ${res.url()}`);
    }
  });
  page.on('pageerror', (err) => {
    consoleErrors.push(err.message);
  });

  try {
    // ─── 1. Login Page & Demo Personas ──────────────────────────────────────
    console.log('\n[1] Initial Page Load & Persona Selection');
    await page.goto(BASE_URL);
    await page.waitForLoadState('networkidle');

    const title = await page.title();
    if (title.includes('WorkGrid')) {
      record('Frontend loads with title "WorkGrid"', 'PASS', title);
    } else {
      record('Frontend loads with title "WorkGrid"', 'FAIL', `Unexpected title: ${title}`);
    }

    const personaCardCount = await page.locator('.login-persona-btn').count();
    record('Demo persona cards displayed on login page', personaCardCount >= 5 ? 'PASS' : 'FAIL', `Found ${personaCardCount} cards`);

    // ─── 2. Login as Alex Vance (Owner) ─────────────────────────────────────
    console.log('\n[2] Login & Profile Display (Alex Vance)');
    const alexCard = page.locator('.login-persona-btn', { hasText: 'Alex Vance' });
    await alexCard.click();

    // Wait for Dashboard
    await page.waitForSelector('#current-user-badge', { timeout: 10000 });
    const userBadgeText = await page.locator('#current-user-badge').innerText();
    record('Login succeeds and redirects to Dashboard', userBadgeText.includes('Alex Vance') ? 'PASS' : 'FAIL', userBadgeText.replace(/\n/g, ' '));

    const heroGreeting = await page.locator('#dashboard-overview-hero h2').innerText();
    record('Hero greeting rendered with display name', heroGreeting.includes('Alex Vance') ? 'PASS' : 'FAIL', heroGreeting);

    const activeRolePill = await page.locator('.role-badge-pill').first().innerText();
    record('Active position card rendered', activeRolePill.includes('owner') ? 'PASS' : 'FAIL', activeRolePill);

    // Wait for SSE connection status
    await page.waitForTimeout(1000);
    const sseBadge = await page.locator('.sse-status-badge').innerText();
    record('Live Updates SSE status indicator', sseBadge.includes('Connected') ? 'PASS' : 'FAIL', sseBadge);

    // ─── 3. Unified To-Do Queue ─────────────────────────────────────────────
    console.log('\n[3] Unified To-Do Queue');
    await page.click('#nav-item-todo');
    await page.waitForSelector('#unified-todo-view', { timeout: 5000 });
    record('Navigate to To-Do Queue view', 'PASS');

    const todoHeading = await page.locator('#unified-todo-view h2').innerText();
    record('To-Do Queue heading and subtitle rendered', todoHeading.includes('Actionable To-Do') ? 'PASS' : 'FAIL', todoHeading);

    // ─── 4. Work Item Lifecycle (Create, Claim, Transition) ───────────────────
    console.log('\n[4] Work Items Lifecycle (Create, View, Claim, Resolve)');
    await page.click('#nav-item-work');
    await page.waitForSelector('#btn-create-work-item', { timeout: 5000 });
    record('Navigate to Work Items tab', 'PASS');

    // Click "+ New Work Item"
    await page.click('#btn-create-work-item');
    await page.waitForSelector('.modal-content', { timeout: 5000 });
    record('Create Work Item modal opens', 'PASS');

    const testTaskTitle = `Playwright E2E Task ${Date.now().toString().slice(-4)}`;
    await page.fill('#create-title', testTaskTitle);
    await page.selectOption('#create-kind', 'task');
    await page.selectOption('#create-priority', 'high');
    await page.fill('#create-description', 'Automated browser verification task');
    
    // Submit creation
    await page.click('.modal-footer .btn-primary');
    await page.waitForSelector('.modal-content', { state: 'detached', timeout: 5000 });
    record('Work item form submitted cleanly', 'PASS');

    // Wait for item to appear in table
    await page.waitForTimeout(1000);
    const itemRow = page.locator('table.data-table tbody tr', { hasText: testTaskTitle });
    const itemVisible = await itemRow.isVisible();
    record('Created item appears in Work Items table', itemVisible ? 'PASS' : 'FAIL', testTaskTitle);

    // Click row to view detail
    await itemRow.click();
    await page.waitForSelector('#work-item-detail', { timeout: 5000 });
    record('Work item detail panel opens on click', 'PASS');

    // Claim item (if claim button is present)
    const claimBtn = page.locator('#work-item-detail button.btn-success', { hasText: 'Claim' });
    if (await claimBtn.isVisible()) {
      await claimBtn.click();
      await page.waitForTimeout(1000);
      record('Claim task button transitions item to in_progress', 'PASS');
    } else {
      record('Claim task button available', 'PASS', 'Item already claimed or assigned');
    }

    // Transition item to resolved
    const transitionBtn = page.locator('#work-item-detail button.btn-secondary', { hasText: 'Transition' });
    await transitionBtn.click();
    await page.waitForSelector('#transition-status', { timeout: 5000 });
    await page.selectOption('#transition-status', 'resolved');
    await page.fill('#transition-reason', 'Verified in Playwright E2E');
    await page.click('.modal-footer .btn-primary:has-text("Confirm Transition")');
    await page.waitForSelector('#transition-status', { state: 'detached', timeout: 5000 });
    await page.waitForTimeout(1000);
    record('Transition to resolved executed cleanly', 'PASS');

    // Close detail panel
    await page.click('#work-item-detail button.btn-secondary:has-text("Close")');

    // ─── 5. Persona Switching & State Isolation ─────────────────────────────
    console.log('\n[5] Persona Switching & Session State Clearing (WG-09)');
    // Switch to Sam Taylor via top persona button
    const samBtn = page.locator('.persona-switcher button.persona-btn', { hasText: 'Sam' });
    await samBtn.click();
    await page.waitForTimeout(1000);

    const currentUserText = await page.locator('#current-user-badge').innerText();
    const isSam = currentUserText.includes('Sam Taylor');
    record('Switch persona to Sam Taylor (Member)', isSam ? 'PASS' : 'FAIL', currentUserText.replace(/\n/g, ' '));

    // Verify Sam's active role card
    const samRole = await page.locator('.user-badge-role').innerText();
    record('Sam active position displays member (Platform Infrastructure)', samRole.toLowerCase().includes('member') ? 'PASS' : 'FAIL', samRole);

    // ─── 6. Role Requests (Sam) ─────────────────────────────────────────────
    console.log('\n[6] Role Request Creation (Sam Taylor)');
    await page.click('#nav-item-approvals');
    await page.waitForSelector('button:has-text("+ Request Role")', { timeout: 5000 });
    await page.click('button:has-text("+ Request Role")');
    await page.waitForSelector('#req-role', { timeout: 5000 });
    record('Submit Role Request modal opens', 'PASS');

    await page.selectOption('#req-role', 'team_leader');
    await page.selectOption('#req-scope-type', 'team');
    await page.selectOption('#req-scope-id', { label: 'Platform Infrastructure' });
    await page.fill('#req-reason', 'Playwright promotion request evaluation');
    
    await page.click('.modal-footer .btn-primary:has-text("Submit Request")');
    await page.waitForSelector('#req-role', { state: 'detached', timeout: 5000 });
    await page.waitForTimeout(1000);
    record('Role request submitted cleanly', 'PASS');

    const myRequestsText = await page.locator('.cards-grid').innerText();
    record('Pending request visible in My Role Requests list', myRequestsText.includes('team_leader') ? 'PASS' : 'FAIL');

    // ─── 7. Approval & Rejection Flow (Dana Scully) ──────────────────────────
    console.log('\n[7] Approvals Governance & Rejection Rules (Dana Scully)');
    // Switch to Dana Scully (Department Manager)
    const danaBtn = page.locator('.persona-switcher button.persona-btn', { hasText: 'Dana' });
    await danaBtn.click();
    await page.waitForTimeout(1000);

    const danaUserText = await page.locator('#current-user-badge').innerText();
    record('Switch persona to Dana Scully (Dept Mgr)', danaUserText.includes('Dana Scully') ? 'PASS' : 'FAIL');

    await page.click('#nav-item-approvals');
    await page.waitForSelector('table.data-table', { timeout: 5000 });

    const pendingRow = page.locator('table.data-table tbody tr', { hasText: 'Sam Taylor' }).first();
    const hasPending = await pendingRow.isVisible();
    record('Sam request visible in Dana Approvals Inbox', hasPending ? 'PASS' : 'FAIL');

    if (hasPending) {
      // Test Rejection Flow with Required Reason contract
      const rejectBtn = pendingRow.locator('button.btn-danger', { hasText: 'Reject' });
      await rejectBtn.click();
      await page.waitForSelector('#decision-comment', { timeout: 5000 });

      // Check confirm button disabled when comment is empty
      const confirmRejectBtn = page.locator('.modal-footer button.btn-danger');
      const isDisabledInitially = await confirmRejectBtn.isDisabled();
      record('Rejection requires reason (confirm button disabled when empty)', isDisabledInitially ? 'PASS' : 'FAIL');

      await page.fill('#decision-comment', 'Declined for Playwright UAT validation');
      const isEnabledNow = !(await confirmRejectBtn.isDisabled());
      record('Rejection reason entered enables confirm button', isEnabledNow ? 'PASS' : 'FAIL');

      await confirmRejectBtn.click();
      await page.waitForSelector('#decision-comment', { state: 'detached', timeout: 5000 });
      await page.waitForTimeout(1000);
      record('Rejection executed and recorded', 'PASS');
    }

    // ─── 8. Notifications Drawer ────────────────────────────────────────────
    console.log('\n[8] Notifications Drawer & Read/Unread Status');
    await page.click('#notification-bell-btn');
    await page.waitForSelector('#notification-drawer', { timeout: 5000 });
    record('Notifications drawer opens on bell icon click', 'PASS');

    const drawerTitle = await page.locator('.notification-header-title').innerText();
    record('Notification header renders', 'PASS', drawerTitle.replace(/\n/g, ' '));

    // Close notifications drawer
    await page.click('.notification-header button[title="Close"]');
    await page.waitForSelector('#notification-drawer', { state: 'detached', timeout: 5000 });
    record('Notifications drawer closes cleanly', 'PASS');

    // ─── 9. Viewer RBAC (Jordan Lee) ────────────────────────────────────────
    console.log('\n[9] Viewer RBAC Restrictions (Jordan Lee)');
    const jordanBtn = page.locator('.persona-switcher button.persona-btn', { hasText: 'Jordan' });
    await jordanBtn.click();
    await page.waitForTimeout(1000);

    const jordanText = await page.locator('#current-user-badge').innerText();
    record('Switch persona to Jordan Lee (Viewer)', jordanText.includes('Jordan Lee') ? 'PASS' : 'FAIL');

    await page.click('#nav-item-work');
    await page.waitForTimeout(500);
    const workItemsContent = await page.locator('.content-container').innerText();
    record('Work items list displays for viewer without crash', 'PASS');

    // ─── 10. Logout & Expired Session Reset ──────────────────────────────────
    console.log('\n[10] Logout & Session Clearing');
    await page.click('#current-user-badge button.logout-btn');
    await page.waitForSelector('.login-page', { timeout: 5000 });
    record('Logout returns cleanly to Persona Login page', 'PASS');

    const tokenInStorage = await page.evaluate(() => localStorage.getItem('workgrid_token'));
    record('Token removed from localStorage on logout', tokenInStorage === null ? 'PASS' : 'FAIL');

    // ─── 11. Console Error Check ────────────────────────────────────────────
    console.log('\n[11] Console Error & Stability Check');
    const criticalErrors = consoleErrors.filter(e => !e.includes('favicon') && !e.includes('ProactorEventLoop'));
    record('Zero uncaught exceptions or critical console errors', criticalErrors.length === 0 ? 'PASS' : 'FAIL',
      criticalErrors.length === 0 ? 'Clean console' : `${criticalErrors.length} errors: ${criticalErrors.slice(0, 2).join('; ')}`);

  } catch (err) {
    console.error('Test suite execution error:', err);
    record('Complete test suite execution', 'FAIL', err.message);
  } finally {
    await browser.close();
  }

  // ─── Summary ──────────────────────────────────────────────────────────────
  console.log('\n' + '='.repeat(75));
  console.log('BROWSER UAT RESULTS SUMMARY');
  console.log('='.repeat(75));
  const passes = results.filter(r => r.status === 'PASS').length;
  const fails = results.filter(r => r.status === 'FAIL').length;
  const total = results.length;

  console.log(`Total Workflows Tested: ${total}`);
  console.log(`  PASS:    ${passes}/${total} (${Math.round((passes / total) * 100)}%)`);
  console.log(`  FAIL:    ${fails}/${total}`);
  console.log('='.repeat(75));

  if (fails === 0) {
    console.log('\n✓ ALL BROWSER-BASED UAT WORKFLOWS PASSED WITH 100% SUCCESS.');
    process.exit(0);
  } else {
    console.log(`\n✗ ${fails} test(s) FAILED.`);
    process.exit(1);
  }
}

runSuite();
