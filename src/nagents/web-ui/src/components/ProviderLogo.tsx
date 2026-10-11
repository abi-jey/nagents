import { Icon } from "./Icon.js";

const logos: Record<string, string> = {
  openai: "openai.svg", anthropic: "anthropic.png", gemini: "gemini.png", gemini_native: "gemini.png",
  foundry: "foundry.ico", azure: "foundry.ico", azure_openai_compatible: "foundry.ico", azure_openai_compatible_v1: "foundry.ico",
  openrouter: "openrouter.png", litellm: "litellm.svg",
};

/** Decorative provider identity; the adjacent name remains the accessible label. */
export function ProviderLogo({ provider }: { provider: string }) {
  const file = Object.hasOwn(logos, provider) ? logos[provider] : "";
  return <span className={`provider-logo${file ? " has-brand" : ""}`} aria-hidden="true">
    {file ? <img src={`/assets/provider-logos/${file}`} alt="" width={20} height={20} decoding="async" /> : <Icon name="channels" size={18} />}
  </span>;
}
