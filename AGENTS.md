### Description
Nagents (this Repository) is provider agnosetic agent framework built to be simple in dependencies and usage and to support different providers and tools and their features.

### Providers
- OpenAI (https://developers.openai.com/api/reference/overview)
- Anthropic (https://platform.claude.com/docs/en/api/overview)
- Google Gemini (https://ai.google.dev/gemini-api/docs)

### Development
You can use the Playwright tools to research API documentation above and use them for implementation.

After implementing the changes the User must be notified on what has changed, and provide an explanation of the implementation.

You can use poetry for dependency management and packaging and running any task should run withing python venv.

#### Pre-commit
After making changes always make sure you run `pre-commit run --all-files` to ensure strict linting and formatting and typing rules.

#### Typing
Try to Avoid using `Any` type and <Type>|None types and use more specific types instead.
