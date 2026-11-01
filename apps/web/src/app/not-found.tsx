import Link from "next/link";

// Our own 404 page (#201). Next's built-in one carries a <style> element of its own, which no
// nonce covers, so the page's Content-Security-Policy refused it (lib/csp.ts).
export default function NotFound() {
  return (
    <main className="flex min-h-screen items-center justify-center px-6 py-12">
      <div className="max-w-sm">
        <p className="font-serif text-xl font-semibold">QuantLab</p>
        <h1 className="mt-6 font-serif text-3xl">Page not found</h1>
        <p className="mt-3 text-sm leading-relaxed text-ink-2">
          There is nothing at this address. It may have been mistyped, or what was here has been deleted.
        </p>
        <Link href="/" className="btn btn-primary mt-6 inline-flex">
          Back to QuantLab
        </Link>
      </div>
    </main>
  );
}
