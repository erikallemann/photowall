const uiLanguage = document.documentElement.lang;
const uiStrings = JSON.parse(document.getElementById('ui-strings').textContent);

function text(message, values = {}) {
  const localized = Object.hasOwn(uiStrings, message) ? uiStrings[message] : message;
  return localized.replace(/\{(\w+)\}/g, (placeholder, name) => values[name] ?? placeholder);
}
