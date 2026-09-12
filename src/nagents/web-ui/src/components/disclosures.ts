export function rememberDisclosure(current: Map<string, boolean>, key: string, open: boolean) {
  if ((current.get(key) ?? false) === open) return current;
  return new Map(current).set(key, open);
}

// Open synchronously before measuring/scrolling/focusing, then return stable keys
// so controlled React disclosures preserve that choice on subsequent updates.
export function revealAncestors(target: HTMLElement, boundary: HTMLElement): string[] {
  const keys: string[] = [];
  let ancestor = target.parentElement;
  while (ancestor && ancestor !== boundary) {
    if (ancestor.tagName === "DETAILS") {
      (ancestor as HTMLDetailsElement).open = true;
      if (ancestor.dataset.disclosureKey) keys.push(ancestor.dataset.disclosureKey);
    }
    ancestor = ancestor.parentElement;
  }
  return keys;
}
