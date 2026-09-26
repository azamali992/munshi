/* Month close: status, checklist, close, reopen with a reason. OWNED BY STREAM C. Stream 0 placeholder (owner only). */
export default async function close(view) {
  view.header('Month close', 'Lock a month once its books are right');
  view.page.innerHTML = '<p class="o-note">Coming soon: the month-close checklist.</p>';
}
