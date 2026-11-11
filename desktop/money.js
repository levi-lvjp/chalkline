export function money(minor, currency = 'USD', locale = 'en-US') {
  if (minor == null) return '—';
  const format = new Intl.NumberFormat(locale, { style: 'currency', currency });
  return format.format(minor / 10 ** format.resolvedOptions().maximumFractionDigits);
}
