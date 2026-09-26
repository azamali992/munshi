/* Accounts and banks: balances, account books, transfers, cash count, reconciliation, method routes. OWNED BY STREAM C.
   Stream 0 placeholder. Salary / advance lines arrive already redacted for non-owners (the API does it). */
export default async function accounts(view) {
  view.header('Accounts & banks', 'Cash, banks and wallets');
  view.page.innerHTML = '<p class="o-note">Coming soon: money accounts, account books and bank reconciliation.</p>';
}
