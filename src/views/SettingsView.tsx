import { useCallback, useEffect, useState } from 'react';

import {
  ApiError,
  BackendApi,
  type LlmModel,
  type LlmProvider,
  type Settings,
} from '../lib/api';
import { Banner } from '../components/Banner';
import { Toast, Toasts } from '../components/Toast';
import { LicensesDialog } from '../components/LicensesDialog';

interface Props {
  api: BackendApi;
  dataDir: string | null;
}

/** Stock footage providers keyed by the config field holding their key list. */
const FOOTAGE_PROVIDERS = [
  { key: 'pexels_api_keys', label: 'Pexels API keys', url: 'https://www.pexels.com/api/' },
  { key: 'pixabay_api_keys', label: 'Pixabay API keys', url: 'https://pixabay.com/api/docs/' },
] as const;

/** Reads a config value as a string, tolerating the nulls and numbers TOML
 *  round-trips produce for fields the UI edits as text. */
function asText(value: unknown): string {
  if (value === null || value === undefined) return '';
  return String(value);
}

/** Config stores these as a TOML array; the UI edits them as one line. */
function keyListToText(value: unknown): string {
  return Array.isArray(value) ? value.map(String).join(', ') : asText(value);
}

/** Turns a config suffix such as "account_id" into a field label. */
function humanizeSuffix(suffix: string): string {
  const words = suffix.replace(/_/g, ' ').trim();
  return words.charAt(0).toUpperCase() + words.slice(1);
}

function textToKeyList(text: string): string[] {
  return text
    .split(',')
    .map((entry) => entry.trim())
    .filter(Boolean);
}

export function SettingsView({ api, dataDir }: Props) {
  const [settings, setSettings] = useState<Settings | null>(null);
  const [providers, setProviders] = useState<LlmProvider[]>([]);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [revealKeys, setRevealKeys] = useState(false);
  const [licensesOpen, setLicensesOpen] = useState(false);

  useEffect(() => {
    let active = true;
    Promise.all([api.getSettings(), api.listLlmProviders()])
      .then(([loadedSettings, providerResult]) => {
        if (!active) return;
        setSettings(loadedSettings);
        setProviders(providerResult.providers);
      })
      .catch((cause: unknown) => {
        if (active) setError(cause instanceof ApiError ? cause.message : String(cause));
      })
      .finally(() => {
        if (active) setLoading(false);
      });
    return () => {
      active = false;
    };
  }, [api]);

  const patchApp = useCallback((key: string, value: unknown) => {
    setSettings((previous) =>
      previous ? { ...previous, app: { ...previous.app, [key]: value } } : previous,
    );
  }, []);

  async function handleSave() {
    if (!settings) return;
    setSaving(true);
    setError(null);
    setNotice(null);
    try {
      // Only the writable sections are sent; whisper and proxy are read-only
      // because save_config() does not persist them.
      const saved = await api.updateSettings({
        app: settings.app,
        azure: settings.azure,
        siliconflow: settings.siliconflow,
        minimax_tts: settings.minimax_tts,
        elevenlabs: settings.elevenlabs,
        chatterbox: settings.chatterbox,
        ui: settings.ui,
      });
      setSettings(saved);
      setNotice('Settings saved.');
    } catch (cause) {
      setError(cause instanceof ApiError ? cause.message : String(cause));
    } finally {
      setSaving(false);
    }
  }

  if (loading) {
    return (
      <>
        <h1 className="page-title">Settings</h1>
        <div className="empty">Loading settings…</div>
      </>
    );
  }

  if (!settings) {
    return (
      <>
        <h1 className="page-title">Settings</h1>
        {error && <Banner tone="error">{error}</Banner>}
      </>
    );
  }

  // OpenRouter, matching config.example.toml: the one provider whose default
  // model costs nothing, so a fresh install is not blocked on a paid account.
  const provider = asText(settings.app.llm_provider) || 'openrouter';
  const providerSpec = providers.find((entry) => entry.provider_id === provider);
  const secretType = revealKeys ? 'text' : 'password';

  return (
    <>
      <h1 className="page-title">Settings</h1>
      <p className="page-subtitle">
        Stored in config.toml inside your app data folder. Keys never leave this machine except
        in requests to the provider you choose.
      </p>

      <Toasts>
        {error && <Toast tone="error" message={error} onDismiss={() => setError(null)} />}
        {notice && <Toast tone="success" message={notice} onDismiss={() => setNotice(null)} />}
      </Toasts>

      <label className="checkbox">
        <input
          type="checkbox"
          checked={revealKeys}
          onChange={(event) => setRevealKeys(event.target.checked)}
        />
        Show API keys
      </label>

      <section className="section">
        <h2 className="section__title">Language model</h2>
        <p className="section__hint">Used to write scripts and pick footage keywords.</p>

        <div className="field">
          <label className="field__label" htmlFor="llm-provider">
            Provider
          </label>
          <select
            id="llm-provider"
            value={provider}
            onChange={(event) => patchApp('llm_provider', event.target.value)}
          >
            {providers.map((entry) => (
              <option key={entry.provider_id} value={entry.provider_id}>
                {entry.label}
              </option>
            ))}
          </select>
        </div>

        {/* Credentials are rendered from the provider's own metadata: config
            keys follow a fixed `<id>_<suffix>` scheme, but which fields apply
            differs per provider (Ollama needs no key, Cloudflare needs an
            account and gateway id). */}
        <div className="grid">
          {providerSpec?.show_api_key !== false && (
            <div className="field">
              <label className="field__label" htmlFor="llm-key">
                API key{providerSpec?.requires_api_key === false && ' (optional)'}
              </label>
              <input
                id="llm-key"
                type={secretType}
                autoComplete="off"
                value={asText(settings.app[`${provider}_api_key`])}
                onChange={(event) => patchApp(`${provider}_api_key`, event.target.value)}
              />
              {providerSpec?.api_key_url && (
                <span className="field__hint">Get one at {providerSpec.api_key_url}</span>
              )}
            </div>
          )}

          <LlmModelField
            // Remounted per provider: both the catalogue and whether the field
            // is being typed into belong to one provider and nothing else.
            key={provider}
            api={api}
            provider={provider}
            spec={providerSpec}
            value={asText(settings.app[`${provider}_model_name`])}
            onChange={(model) => patchApp(`${provider}_model_name`, model)}
          />

          {providerSpec?.show_base_url !== false && (
            <div className="field">
              <label className="field__label" htmlFor="llm-base-url">
                Base URL
              </label>
              <input
                id="llm-base-url"
                type="text"
                value={asText(settings.app[`${provider}_base_url`])}
                placeholder={providerSpec?.default_base_url || 'provider default'}
                onChange={(event) => patchApp(`${provider}_base_url`, event.target.value)}
              />
              <span className="field__hint">Leave empty unless you use a proxy or gateway.</span>
            </div>
          )}

          {providerSpec?.extra_fields.map((field) => {
            const configKey = `${provider}_${field.config_suffix}`;
            return (
              <div className="field" key={configKey}>
                <label className="field__label" htmlFor={configKey}>
                  {humanizeSuffix(field.config_suffix)}
                  {!field.required && ' (optional)'}
                </label>
                <input
                  id={configKey}
                  type={field.secret ? secretType : 'text'}
                  autoComplete="off"
                  value={asText(settings.app[configKey])}
                  placeholder={field.default_value}
                  onChange={(event) => patchApp(configKey, event.target.value)}
                />
              </div>
            );
          })}
        </div>
      </section>

      <section className="section">
        <h2 className="section__title">Stock footage</h2>
        <p className="section__hint">
          At least one provider key is required unless you render from local files.
        </p>

        {FOOTAGE_PROVIDERS.map((entry) => (
          <div className="field" key={entry.key}>
            <label className="field__label" htmlFor={entry.key}>
              {entry.label}
            </label>
            <input
              id={entry.key}
              type={secretType}
              autoComplete="off"
              value={keyListToText(settings.app[entry.key])}
              placeholder="key1, key2"
              onChange={(event) => patchApp(entry.key, textToKeyList(event.target.value))}
            />
            <span className="field__hint">
              Comma separated; the backend rotates between them. Get one at {entry.url}
            </span>
          </div>
        ))}
      </section>

      <section className="section">
        <h2 className="section__title">Subtitles</h2>

        <div className="grid">
          <div className="field">
            <label className="field__label" htmlFor="subtitle-provider">
              Transcription engine
            </label>
            <select
              id="subtitle-provider"
              value={asText(settings.app.subtitle_provider) || 'edge'}
              onChange={(event) => patchApp('subtitle_provider', event.target.value)}
            >
              <option value="edge">Edge TTS timings (fast, no download)</option>
              <option value="whisper">Whisper (accurate, downloads a model)</option>
            </select>
            <span className="field__hint">
              Whisper downloads its model on first use and runs locally on the CPU.
            </span>
          </div>
        </div>
      </section>

      <section className="section">
        <h2 className="section__title">Premium voices</h2>
        <p className="section__hint">
          Optional. Edge TTS voices work without any key. Fill either of these in
          and the matching engine becomes selectable under Voice engine when you
          generate a video.
        </p>

        <div className="grid">
          <div className="field">
            <label className="field__label" htmlFor="azure-key">
              Azure Speech key
            </label>
            <input
              id="azure-key"
              type={secretType}
              autoComplete="off"
              value={asText(settings.azure.speech_key)}
              onChange={(event) =>
                setSettings((previous) =>
                  previous
                    ? { ...previous, azure: { ...previous.azure, speech_key: event.target.value } }
                    : previous,
                )
              }
            />
            <span className="field__hint">
              Unlocks the multilingual V2 voices. This is a Speech resource key,
              not an Azure OpenAI one. Get one at portal.azure.com
            </span>
          </div>

          <div className="field">
            <label className="field__label" htmlFor="azure-region">
              Azure Speech region
            </label>
            <input
              id="azure-region"
              type="text"
              placeholder="westeurope"
              value={asText(settings.azure.speech_region)}
              onChange={(event) =>
                setSettings((previous) =>
                  previous
                    ? {
                        ...previous,
                        azure: { ...previous.azure, speech_region: event.target.value },
                      }
                    : previous,
                )
              }
            />
            <span className="field__hint">
              Required alongside the key: the SDK cannot connect without it.
            </span>
          </div>

          <div className="field">
            <label className="field__label" htmlFor="elevenlabs-key">
              ElevenLabs API key
            </label>
            <input
              id="elevenlabs-key"
              type={secretType}
              autoComplete="off"
              value={asText(settings.elevenlabs.api_key)}
              onChange={(event) =>
                setSettings((previous) =>
                  previous
                    ? {
                        ...previous,
                        elevenlabs: { ...previous.elevenlabs, api_key: event.target.value },
                      }
                    : previous,
                )
              }
            />
            <span className="field__hint">
              Lists the voices on your account. Get one at
              elevenlabs.io/app/settings/api-keys
            </span>
          </div>

          <div className="field">
            <label className="field__label" htmlFor="elevenlabs-model">
              ElevenLabs model
            </label>
            <input
              id="elevenlabs-model"
              type="text"
              placeholder="eleven_multilingual_v2"
              value={asText(settings.elevenlabs.model_id)}
              onChange={(event) =>
                setSettings((previous) =>
                  previous
                    ? {
                        ...previous,
                        elevenlabs: { ...previous.elevenlabs, model_id: event.target.value },
                      }
                    : previous,
                )
              }
            />
            <span className="field__hint">
              Which model speaks the lines: eleven_multilingual_v2,
              eleven_flash_v2_5 or eleven_v3.
            </span>
          </div>
        </div>
      </section>

      <section className="section">
        <h2 className="section__title">Storage</h2>
        <div className="field">
          <span className="field__label">App data folder</span>
          <code style={{ fontSize: 12, wordBreak: 'break-all' }}>{dataDir ?? 'unknown'}</code>
          <span className="field__hint">
            Renders, downloaded footage and config.toml live here.
          </span>
        </div>
      </section>

      <section className="section">
        <h2 className="section__title">About</h2>
        <div className="field">
          <span className="field__label">Licenses</span>
          <span className="field__hint">
            Clip Architect is free software under the GNU GPL v3, and it bundles
            FFmpeg, two open fonts and a Python runtime, each under its own terms.
            Every one of those licence texts is installed with the app and can be
            read here.
          </span>
          <div className="button-row">
            <button
              type="button"
              className="button"
              onClick={() => setLicensesOpen(true)}
            >
              View licenses
            </button>
          </div>
        </div>
      </section>

      <div className="button-row">
        <button className="button button--primary" onClick={handleSave} disabled={saving}>
          {saving ? 'Saving…' : 'Save settings'}
        </button>
      </div>

      {licensesOpen && <LicensesDialog onClose={() => setLicensesOpen(false)} />}
    </>
  );
}

/** Sentinel for the "type it yourself" entry. No provider serves a model
 *  under this id, so it cannot collide with a real one. */
const CUSTOM_MODEL = '__custom_model__';

/** Models grouped by the vendor prefix in their id (`openai/gpt-5.5`), which
 *  is what makes a list of several hundred navigable. An id with no prefix
 *  keeps its own group rather than being filed under a made-up vendor. */
function groupByVendor(
  models: LlmModel[],
): Array<{ vendor: string; models: LlmModel[] }> {
  const groups = new Map<string, LlmModel[]>();
  for (const model of models) {
    const slash = model.id.indexOf('/');
    const vendor = slash > 0 ? model.id.slice(0, slash) : 'Other';
    const bucket = groups.get(vendor);
    if (bucket) bucket.push(model);
    else groups.set(vendor, [model]);
  }
  return [...groups.entries()]
    .sort((left, right) => left[0].localeCompare(right[0]))
    .map(([vendor, entries]) => ({ vendor, models: entries }));
}

/** The model id: picked from the provider's catalogue, or typed by hand.
 *
 *  Only providers that publish a catalogue get a list; the rest keep the
 *  plain text field they always had. The list never replaces typing - an
 *  aggregator's catalogue is not the whole truth about what it will route,
 *  models appear in it late, and some are private to an account - so "Custom
 *  model" stays one choice away, and a saved id the catalogue does not list
 *  opens the text field rather than being silently swapped for whatever the
 *  list happens to show first. */
function LlmModelField({
  api,
  provider,
  spec,
  value,
  onChange,
}: {
  api: BackendApi;
  provider: string;
  spec: LlmProvider | undefined;
  value: string;
  onChange: (model: string) => void;
}) {
  const [models, setModels] = useState<LlmModel[]>([]);
  const [freeOnly, setFreeOnly] = useState(true);
  const [catalogError, setCatalogError] = useState<string | null>(null);
  const [loadingCatalog, setLoadingCatalog] = useState(false);
  const [custom, setCustom] = useState(false);

  const hasCatalog = spec?.has_model_catalog === true;

  useEffect(() => {
    if (!hasCatalog) return;
    let active = true;
    // This effect is the network fetch, which is what effects are for; the
    // flag only says it is in flight.
    // oxlint-disable-next-line react/set-state-in-effect
    setLoadingCatalog(true);
    api
      .listLlmModels(provider)
      .then((result) => {
        if (!active) return;
        setModels(result.models);
        setCatalogError(result.error);
      })
      .catch((cause: unknown) => {
        if (active) {
          setCatalogError(cause instanceof ApiError ? cause.message : String(cause));
        }
      })
      .finally(() => {
        if (active) setLoadingCatalog(false);
      });
    return () => {
      active = false;
    };
  }, [api, provider, hasCatalog]);

  const listed = models.some((model) => model.id === value);
  // A saved id the catalogue does not carry is exactly what the text field is
  // for, so the field opens on it without the user having to ask.
  const typing = custom || (models.length > 0 && value !== '' && !listed);

  const freeCount = models.filter((model) => model.free).length;
  // Several hundred models, of which around twenty cost nothing. Scanning for
  // those by hand is the kind of task that sends someone back to a paid
  // account out of fatigue, so the filter starts on wherever free models
  // exist. The currently selected model is always kept in the list: filtering
  // a chosen paid model out from under the select would silently reset it.
  const visibleModels =
    freeOnly && freeCount > 0
      ? models.filter((model) => model.free || model.id === value)
      : models;

  if (models.length === 0 || typing) {
    return (
      <div className="field">
        <label className="field__label" htmlFor="llm-model">
          Model name
        </label>
        <input
          id="llm-model"
          type="text"
          value={value}
          placeholder={spec?.default_model || 'provider default'}
          onChange={(event) => onChange(event.target.value)}
        />
        {loadingCatalog && <span className="field__hint">Loading the model list…</span>}
        {catalogError && (
          <span className="field__hint">
            The model list could not be loaded ({catalogError}). Type a model id
            instead.
          </span>
        )}
        {models.length > 0 && (
          <button
            type="button"
            className="button"
            style={{ alignSelf: 'flex-start', marginTop: 4 }}
            onClick={() => {
              // Going back means giving up the typed id: keeping it would put
              // the field straight back into typing mode, since not listing it
              // is precisely what the catalogue does.
              onChange('');
              setCustom(false);
            }}
          >
            Pick from the catalogue instead
          </button>
        )}
      </div>
    );
  }

  return (
    <div className="field">
      <label className="field__label" htmlFor="llm-model">
        Model
      </label>
      <select
        id="llm-model"
        value={listed ? value : ''}
        onChange={(event) => {
          if (event.target.value === CUSTOM_MODEL) {
            setCustom(true);
            return;
          }
          onChange(event.target.value);
        }}
      >
        {!listed && (
          <option value="">
            {spec?.default_model
              ? `Provider default (${spec.default_model})`
              : 'Provider default'}
          </option>
        )}
        {groupByVendor(visibleModels).map((group) => (
          <optgroup key={group.vendor} label={group.vendor}>
            {group.models.map((model) => (
              <option key={model.id} value={model.id}>
                {model.label}
                {model.free ? ' — free' : ''}
              </option>
            ))}
          </optgroup>
        ))}
        <option value={CUSTOM_MODEL}>Custom model…</option>
      </select>
      {freeCount > 0 && (
        <label className="checkbox">
          <input
            type="checkbox"
            checked={freeOnly}
            onChange={(event) => setFreeOnly(event.target.checked)}
          />
          Only models that cost nothing
        </label>
      )}
      <span className="field__hint">
        {freeOnly && freeCount > 0
          ? `${freeCount} of ${models.length} models from this provider cost nothing to call. `
          : `${models.length} model${models.length === 1 ? '' : 's'} published by this provider. `}
        Not listed? Choose "Custom model…" and paste the id.
      </span>
      {catalogError && (
        <span className="field__hint">This list may be out of date: {catalogError}</span>
      )}
    </div>
  );
}
