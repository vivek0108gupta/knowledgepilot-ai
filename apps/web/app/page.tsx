import { ApiStatus } from "@/components/api-status";

export default function Home() {
  return (
    <main className="flex flex-1 flex-col items-center justify-center gap-6 px-6 py-24 text-center">
      <h1 className="text-4xl font-semibold tracking-tight sm:text-5xl">KnowledgePilot AI</h1>
      <p className="max-w-xl text-lg text-zinc-600 dark:text-zinc-400">
        Upload your documents and ask questions answered from them, with citations back to the source page.
      </p>
      <ApiStatus />
    </main>
  );
}
