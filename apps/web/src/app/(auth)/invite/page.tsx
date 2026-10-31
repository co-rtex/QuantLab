import type { Metadata } from "next";
import { InviteResponse } from "./InviteResponse";

export const metadata: Metadata = { title: "Workspace invitation" };

export default function InvitePage() {
  return (
    <>
      <h1 className="mb-6 font-serif text-2xl font-semibold">Workspace invitation</h1>
      <InviteResponse />
    </>
  );
}
