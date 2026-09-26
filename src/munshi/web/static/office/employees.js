/* Employees: list, add / edit, app login (PIN shown once), end employment, rehire. OWNED BY STREAM C.
   Stream 0 placeholder. Pay fields (basic, rate, components, commission, advances) render only when can('payroll:read'):
   salaries are the owner's alone. */
import { can } from './lib.js';

export default async function employees(view) {
  view.header('Employees', can('payroll:read') ? 'People, pay terms and app logins' : 'People and app logins');
  view.page.innerHTML = '<p class="o-note">Coming soon: the employee list, pay terms and app logins.</p>';
}
