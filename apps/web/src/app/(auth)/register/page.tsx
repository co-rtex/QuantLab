import type { Metadata } from "next";
import { Suspense } from "react";
import { AuthForm } from "../AuthForm";

export const metadata: Metadata = { title: "Create account" };

export default function RegisterPage() {
  return (
    <>
      <h1 className="mb-6 font-serif text-2xl font-semibold">Create your account</h1>
      <Suspense>
        <AuthForm mode="register" />
      </Suspense>
    </>
  );
}
