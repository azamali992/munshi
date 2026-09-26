/* Phone-app money screens: my payslips (payroll:self), the owner's Money card, the change-PIN sheet, payment-proof
   capture. OWNED BY STREAM C (proof capture UI with Stream E). Stream 0 placeholder.
   Loaded by views.js with a dynamic import() (views.js is a classic script, so a static import is not possible); it
   runs AFTER views.js, and extends the same globals: window.M (core helpers) and window.V (screens). */
const V = window.V || (window.V = {});
export default V;
