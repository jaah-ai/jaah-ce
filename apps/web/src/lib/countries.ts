/** ISO 3166-1 alpha-2 countries for an address's country picker, and the EU VAT prefix rule for a billing
 *  tax id. Names come from the browser (`Intl.DisplayNames`), so only the codes live here. */

const CODES = (
  'AD AE AF AG AI AL AM AO AQ AR AS AT AU AW AX AZ BA BB BD BE BF BG BH BI BJ BL BM BN BO BQ BR BS BT BV BW BY BZ ' +
  'CA CC CD CF CG CH CI CK CL CM CN CO CR CU CV CW CX CY CZ DE DJ DK DM DO DZ EC EE EG EH ER ES ET FI FJ FK FM FO ' +
  'FR GA GB GD GE GF GG GH GI GL GM GN GP GQ GR GS GT GU GW GY HK HM HN HR HT HU ID IE IL IM IN IO IQ IR IS IT JE ' +
  'JM JO JP KE KG KH KI KM KN KP KR KW KY KZ LA LB LC LI LK LR LS LT LU LV LY MA MC MD ME MF MG MH MK ML MM MN MO ' +
  'MP MQ MR MS MT MU MV MW MX MY MZ NA NC NE NF NG NI NL NO NP NR NU NZ OM PA PE PF PG PH PK PL PM PN PR PS PT PW ' +
  'PY QA RE RO RS RU RW SA SB SC SD SE SG SH SI SJ SK SL SM SN SO SR SS ST SV SX SY SZ TC TD TF TG TH TJ TK TL TM ' +
  'TN TO TR TT TV TW TZ UA UG UM US UY UZ VA VC VE VG VI VN VU WF WS XK YE YT ZA ZM ZW'
).split(' ')

const names = new Intl.DisplayNames(['en'], { type: 'region' })

/** The country's English name, or the code itself when the browser has none. */
export function countryName(code: string): string {
  try {
    return names.of(code) ?? code
  } catch {
    return code
  }
}

export interface Country {
  code: string
  name: string
}

/** Every country, by name. */
export const COUNTRIES: readonly Country[] = CODES.map(code => ({ code, name: countryName(code) })).sort((a, b) =>
  a.name.localeCompare(b.name),
)

/** Common names a search should find that are neither the country's name nor its ISO code. */
const ALIASES: Readonly<Record<string, string>> = { UK: 'GB', USA: 'US', UAE: 'AE', EL: 'GR' }

/** Whether a picker search matches the country: part of its name, its exact code, or an alias. */
export function countryMatches(country: Country, search: string): boolean {
  const q = search.trim().toLowerCase()
  if (!q) return true
  return country.name.toLowerCase().includes(q) || country.code.toLowerCase() === q || ALIASES[q.toUpperCase()] === country.code
}

/** EU member states → the prefix their VAT numbers carry. Greece is `EL`, not its ISO `GR`. */
const EU_VAT_PREFIX: Readonly<Record<string, string>> = {
  AT: 'AT', BE: 'BE', BG: 'BG', CY: 'CY', CZ: 'CZ', DE: 'DE', DK: 'DK', EE: 'EE', ES: 'ES', FI: 'FI', FR: 'FR',
  GR: 'EL', HR: 'HR', HU: 'HU', IE: 'IE', IT: 'IT', LT: 'LT', LU: 'LU', LV: 'LV', MT: 'MT', NL: 'NL', PL: 'PL',
  PT: 'PT', RO: 'RO', SE: 'SE', SI: 'SI', SK: 'SK',
}
/** Every prefix an EU VAT number may start with: the member states', plus `XI` for Northern Ireland. */
const VAT_PREFIXES = new Set([...Object.values(EU_VAT_PREFIX), 'XI'])

/** The VAT prefix of an EU member state, or null for any other country. */
export function euVatPrefix(country: string): string | null {
  return EU_VAT_PREFIX[country] ?? null
}

/** Whether a tax id value is an EU VAT prefix alone — what the form prefills, so not yet a number. */
export function isBareVatPrefix(value: string): boolean {
  return VAT_PREFIXES.has(value.trim().toUpperCase())
}

/** Why an EU VAT number would be refused, or null when its shape is right: a known EU prefix followed by 2–12 letters or digits. Spaces, dots and dashes are ignored,
 *  as Stripe ignores them. Any EU prefix passes, not only the address country's: a business may be VAT-registered
 *  in another member state. */
export function euVatProblem(value: string, country: string): string | null {
  const vat = value.toUpperCase().replace(/[\s.-]/g, '')
  const own = euVatPrefix(country)
  const prefix = vat.slice(0, 2)
  if (!VAT_PREFIXES.has(prefix)) return own ? `Start with ${own}` : 'Start with the country prefix, e.g. DE'
  if (!/^[A-Z0-9]{2,12}$/.test(vat.slice(2))) return 'Add the number after the prefix'
  return null
}
