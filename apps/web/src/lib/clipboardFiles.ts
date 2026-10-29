/* The files on a paste's clipboard: images only, unless `anyType`. `kind === 'file'` separates a
   screenshot from a copied <img>, whose `image/*` entry is an HTML string. */
export function clipboardFiles(clipboard: DataTransfer | null, anyType = false): File[] {
  return Array.from(clipboard?.items ?? [])
    .filter(item => item.kind === 'file' && (anyType || item.type.startsWith('image/')))
    .flatMap(item => item.getAsFile() ?? [])
}
