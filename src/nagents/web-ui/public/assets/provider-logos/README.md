# Provider logos

These official provider assets are bundled locally so ngn's web UI never needs
to contact an external image host. Vite copies this directory into
`nagents/web/static/assets/provider-logos` in the Python wheel; the source assets
are also included in the source distribution.

Assets retrieved on 2026-10-11 from the providers' own sites:

| File | Source |
| --- | --- |
| `openai.svg` | Black monoblossom from [OpenAI's logo pack](https://cdn.openai.com/brand/OpenAI-Logos-2025.zip), linked by its [brand guidelines](https://openai.com/brand/) |
| `anthropic.png` | [Anthropic's favicon](https://cdn.prod.website-files.com/67ce28cfec624e2b733f8a52/681d52619fec35886a7f1a70_favicon.png), linked by [anthropic.com](https://www.anthropic.com/) |
| `gemini.png` | [Gemini's icon](https://www.gstatic.com/lamda/images/gemini_sparkle_4g_512_lt_f94943af3be039176192d.png), linked by [gemini.google.com](https://gemini.google.com/) |
| `foundry.ico` | [Microsoft Foundry's favicon](https://ai.azure.com/favicon.ico), used for Foundry and Azure connections |
| `openrouter.png` | [OpenRouter's icon](https://openrouter.ai/favicon/glyph.png), linked by [openrouter.ai](https://openrouter.ai/) |
| `litellm.svg` | [LiteLLM's favicon](https://docs.litellm.ai/img/brand/litellm-favicon.svg), linked by its [documentation](https://docs.litellm.ai/) |

The marks belong to their respective owners and identify provider connections;
they do not imply endorsement of ngn. They are not covered by ngn's MIT license.
Custom and OpenAI-compatible endpoints use a neutral connection icon rather than
the OpenAI mark. Images retain their original artwork and proportions.
