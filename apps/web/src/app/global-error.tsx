"use client"; // error boundaries are client components

import "./globals.css";

// Shown when the root layout itself fails, in its place: hence its own <html> and <body>.
// Ours rather than Next's built-in one (#201), whose <style> element no nonce covers, so the
// page's Content-Security-Policy refused it (lib/csp.ts). App classes only, no style
// attributes or elements: global-error.test.mts checks what it renders.
export default function GlobalError({ retry }: { error: unknown; retry: () => void }) {
  return (
    <html lang="en">
      <body className="min-h-screen antialiased">
        <main className="flex min-h-screen items-center justify-center px-6 py-12">
          <div className="max-w-sm">
            <p className="font-serif text-xl font-semibold">QuantLab</p>
            <h1 className="mt-6 font-serif text-3xl">Something went wrong</h1>
            <p className="mt-3 text-sm leading-relaxed text-ink-2">
              The page could not be shown. Try again, or reload it if that does not help.
            </p>
            <button type="button" className="btn btn-primary mt-6" onClick={() => retry()}>
              Try again
            </button>
          </div>
        </main>
      </body>
    </html>
  );
}
