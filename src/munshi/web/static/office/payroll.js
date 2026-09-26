/* Payroll: the monthly wizard (attendance, adjustments, preview, approve, pay, payslips, statutory). OWNED BY STREAM C.
   Stream 0 placeholder. A clerk (attendance:write without payroll:read) gets the attendance grid ONLY -- no rupee. */
import { can } from './lib.js';

export default async function payroll(view) {
  const full = can('payroll:read');
  view.header('Payroll', full ? 'Attendance, preview, approve, pay, payslips' : 'Attendance');
  view.page.innerHTML = `<p class="o-note">Coming soon: ${full ? 'the monthly payroll wizard' : 'the monthly attendance register'}.</p>`;
}
