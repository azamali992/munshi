/* Finance: P&L, balance sheet, cash flow, trial balance, KPIs, margins, fixed assets, loans, capital and drawings,
   journal, general-journal export. OWNED BY STREAM C. Stream 0 placeholder (owner only: finance:read). */
export default async function finance(view) {
  view.header('Finance', 'Profit and loss, balance sheet, cash flow');
  view.page.innerHTML = '<p class="o-note">Coming soon: the company\'s statements.</p>';
}
