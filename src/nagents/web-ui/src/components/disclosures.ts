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

// Validation can target a control inside a closed optional/JSON section. Open
// its ancestors synchronously, retain that choice, and only then move focus.
export function revealInvalidField(boundary: HTMLElement, remember: (key: string, open: boolean) => void): boolean {
  const invalid = boundary.querySelector<HTMLElement>('[aria-invalid="true"]:not(:disabled)');
  if (!invalid) return false;
  for (const key of revealAncestors(invalid, boundary)) remember(key, true);
  invalid.focus();
  return true;
}
