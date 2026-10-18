import { useEffect, useRef, type ReactNode } from 'react';
import { createPortal } from 'react-dom';
import { Banner } from './Banner';

// A success line has said everything once it has been seen; an error is
// something to act on, so it waits for the user.
const SUCCESS_VISIBLE_MS = 6000;

/**
 * Page-level messages, pinned to the window rather than the top of the page.
 *
 * In a banner above the form, a failure from a button far down the page - the
 * voice-over, the render - landed off screen, and the button just appeared to
 * do nothing. Portalled to the body so no scrolling or transformed ancestor
 * can carry it out of view; one stack, so an error and a notice do not land on
 * top of each other.
 */
export function Toasts({ children }: { children: ReactNode }) {
  return createPortal(
    <div className="toasts" aria-live="polite">
      {children}
    </div>,
    document.body,
  );
}

interface Props {
  tone: 'error' | 'success' | 'warning';
  message: string;
  onDismiss: () => void;
}

export function Toast({ tone, message, onDismiss }: Props) {
  // Views re-render on every task poll and pass a fresh arrow each time;
  // depending on it would restart the timer forever.
  const dismiss = useRef(onDismiss);
  useEffect(() => {
    dismiss.current = onDismiss;
  });

  useEffect(() => {
    if (tone !== 'success') return;
    const timer = window.setTimeout(() => dismiss.current(), SUCCESS_VISIBLE_MS);
    return () => window.clearTimeout(timer);
  }, [tone, message]);

  return (
    <Banner tone={tone} onDismiss={onDismiss}>
      {message}
    </Banner>
  );
}
