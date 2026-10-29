// Shared TanStack query gate: pending shows the usual Loading text, a failed fetch shows a
// red error line with a Retry button, and children (including empty states) render only on
// success, so a broken fetch can never look like "nothing here".
import type { UseQueryResult } from "@tanstack/react-query";
import type { ReactNode } from "react";

export default function QueryState({
  query,
  label,
  children,
}: {
  query: UseQueryResult | UseQueryResult[];
  label: string;
  children: ReactNode;
}) {
  const queries = Array.isArray(query) ? query : [query];
  const failed = queries.filter((q) => q.isError);
  if (failed.length > 0) {
    const msg = failed.map((q) => q.error?.message ?? "unknown error").join("; ");
    return (
      <p className="text-xs text-red-700">
        Couldn&apos;t load {label}: {msg}{" "}
        <button
          onClick={() => failed.forEach((q) => void q.refetch())}
          className="font-semibold text-indigo-600 hover:underline"
        >
          Retry
        </button>
      </p>
    );
  }
  if (queries.some((q) => q.isPending)) return <p className="text-xs text-slate-500">Loading…</p>;
  return <>{children}</>;
}
