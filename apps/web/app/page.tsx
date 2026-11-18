import Link from "next/link";

import { ApiStatus } from "@/components/api-status";
import { Brand } from "@/components/brand";
import { buttonClass } from "@/components/ui/button";
import { LOGIN_PATH, REGISTER_PATH } from "@/lib/auth/routes";

export default function Home() {
  return (
    <div className="flex flex-1 flex-col">
      <header className="flex items-center justify-between px-6 py-4">
        <Brand />
        <Link href={LOGIN_PATH} className={buttonClass("ghost", "sm")}>
          Sign in
        </Link>
      </header>
      <main className="flex flex-1 flex-col items-center justify-center gap-6 px-6 py-24 text-center">
        <h1 className="text-4xl font-semibold tracking-tight text-zinc-900 sm:text-5xl">KnowledgePilot AI</h1>
        <p className="max-w-xl text-lg text-zinc-600">
          Upload your documents and ask questions answered from them, with citations back to the source page.
        </p>
        <div className="flex flex-wrap justify-center gap-3">
          <Link href={REGISTER_PATH} className={buttonClass("primary")}>
            Get started
          </Link>
          <Link href={LOGIN_PATH} className={buttonClass("secondary")}>
            Sign in
          </Link>
        </div>
        <ApiStatus />
      </main>
    </div>
  );
}
