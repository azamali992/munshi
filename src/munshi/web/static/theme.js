/* Runs in <head>, before first paint, so a user who opted into the dark theme never sees a light flash.
   Light is the default (readable in sunlight); dark is only ever an explicit choice saved in Settings.
   A separate file because the CSP is script-src 'self' (no inline scripts). The browser-chrome colour is read
   from the --ground token, so styles.css stays the only place colours are defined. */
window.munshiApplyTheme = function (theme) {
  var root = document.documentElement;
  if (theme === 'dark') root.setAttribute('data-theme', 'dark'); else root.removeAttribute('data-theme');
  var m = document.getElementById('themeColor'), g = getComputedStyle(root).getPropertyValue('--ground').trim();
  if (m && g) m.setAttribute('content', g);
};
(function () {
  var theme = 'light';
  try { theme = JSON.parse(localStorage.getItem('munshi.theme')) || 'light'; } catch (e) { }
  window.munshiApplyTheme(theme);
})();
