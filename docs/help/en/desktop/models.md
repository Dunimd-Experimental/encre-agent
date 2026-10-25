Models are the agent's "brain". Manage all your models in one place under **Settings → Models**.

## Adding a model

Click **New** and fill in the fields:

| Field | Description |
| --- | --- |
| Display name | The name you see in the list (e.g. `DeepSeek`) |
| Model ID | The provider's model identifier (e.g. `deepseek-chat`) |
| Provider | The model provider |
| API key | The key from the provider's console |
| Base URL | Endpoint (API paths are appended automatically on save) |
| Max tokens | Maximum output length per reply |
| Multimodal | Turn on for image-capable models (verified on save) |
| Thinking level | Enable for models with deep reasoning; pick a level next to the input box |

The screen also offers "Get API key" and "View docs" shortcuts.

![Model management page](/screenshots/models-list.png)

## Managing models

- **Enable / Disable**: disabled models don't appear in the picker
- **Edit**: change configuration
- **Delete**: remove the model (doesn't affect your key at the provider)
- Configure models from **multiple providers** and switch anytime at the input box

Encre Agent has built-in support for **31 major LLM providers**, all managed in one app.

## Two ways to choose a model

1. **The picker next to the input box**: choose a model for the current session, switch at will
2. **The automation "execution model"**: pick a dedicated model for scheduled tasks (see [Automation](/en/desktop/automation))

> [!TIP]
> Enable the multimodal toggle for models that accept images, or images may be handled as text even if the model supports them.