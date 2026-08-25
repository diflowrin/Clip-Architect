import type { ReactNode } from 'react';

interface Props {
  tone: 'error' | 'success' | 'warning';
  children: ReactNode;
  onDismiss?: () => void;
}

export function Banner({ tone, children, onDismiss }: Props) {
  return (
    <div className={`banner banner--${tone}`} role={tone === 'error' ? 'alert' : 'status'}>
      <div className="banner__body">{children}</div>
      {onDismiss && (
        <button
          type="button"
          className="button button--danger"
          style={{ marginLeft: 'auto', padding: '0 6px' }}
          onClick={onDismiss}
          aria-label="Dismiss"
        >
          ✕
        </button>
      )}
    </div>
  );
}
