import { useEffect, useState } from 'react';
import { invoke } from '@tauri-apps/api/core';

interface LicenseDocument {
  name: string;
  title: string;
}

/** The licence texts that ship with the app, read from the install folder.
 *
 *  Rendered here rather than opened in Explorer: an MSIX lives under
 *  WindowsApps, which the user is not allowed to browse, so the folder route
 *  ends in an access-denied dialog. */
export function LicensesDialog({ onClose }: { onClose: () => void }) {
  const [documents, setDocuments] = useState<LicenseDocument[]>([]);
  const [selected, setSelected] = useState<string | null>(null);
  // Carries the name it was read for, so switching documents shows the
  // loading line instead of the previous licence under the new title.
  const [content, setContent] = useState<{ name: string; text: string } | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    function onKey(event: KeyboardEvent) {
      if (event.key === 'Escape') onClose();
    }
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [onClose]);

  useEffect(() => {
    let active = true;
    invoke<LicenseDocument[]>('license_documents')
      .then((result) => {
        if (!active) return;
        setDocuments(result);
        setSelected(result[0]?.name ?? null);
      })
      .catch((cause: unknown) => {
        if (active) setError(String(cause));
      });
    return () => {
      active = false;
    };
  }, []);

  useEffect(() => {
    if (!selected) return;
    let active = true;
    invoke<string>('read_license_document', { name: selected })
      .then((result) => {
        if (active) setContent({ name: selected, text: result });
      })
      .catch((cause: unknown) => {
        if (active) setError(String(cause));
      });
    return () => {
      active = false;
    };
  }, [selected]);

  const text = content?.name === selected ? content.text : null;

  return (
    <div
      className="licenses"
      role="dialog"
      aria-modal="true"
      aria-label="Licenses"
      onClick={onClose}
    >
      <div className="licenses__inner" onClick={(event) => event.stopPropagation()}>
        <header className="licenses__bar">
          <div>
            <h2 className="licenses__title">Licenses</h2>
            <p className="licenses__subtitle">
              Clip Architect is free software under the GNU GPL v3, built on other
              people's free software. Every licence it ships under is here, and the
              same files are installed beside the application.
            </p>
          </div>
          <button type="button" className="button" onClick={onClose} autoFocus>
            Close
          </button>
        </header>

        {error && <p className="licenses__error">{error}</p>}

        <div className="licenses__body">
          <nav className="licenses__list" aria-label="Licence documents">
            {documents.map((document) => (
              <button
                key={document.name}
                type="button"
                className="licenses__item"
                aria-current={document.name === selected ? 'true' : undefined}
                onClick={() => setSelected(document.name)}
              >
                <span className="licenses__item-title">{document.title}</span>
                <span className="licenses__item-name">{document.name}</span>
              </button>
            ))}
          </nav>

          {/* Keyed on the file so switching documents scrolls back to the top
              rather than leaving the reader 400 lines into the previous one. */}
          <pre key={selected ?? ''} className="licenses__text" tabIndex={0}>
            {text ?? 'Loading…'}
          </pre>
        </div>
      </div>
    </div>
  );
}
