// A checked claim (design doc 7.4): a chip that opens the tool result it cites, with the cited
// path highlighted.
import { useState } from "react";
import type { ClaimOut, ToolCallOut } from "../../api/client";

export default function ClaimChip({ claim, calls }: { claim: ClaimOut; calls?: ToolCallOut[] }) {
  const [open, setOpen] = useState(false);
  const call = calls?.find((c) => c.call_id === claim.call_id);
  const label = claim.text || String(claim.value);
  return (
    <span className="inline-block">
      <button
        onClick={() => setOpen((o) => !o)}
        title={`${claim.call_id}.${claim.path} = ${JSON.stringify(claim.value)}`}
        className="rounded-full bg-emerald-50 px-2 py-0.5 text-[11px] text-emerald-800 ring-1 ring-emerald-200 hover:bg-emerald-100"
      >
        ✓ {label}
      </button>
      {open && (
        <div className="mt-1 max-w-full rounded border border-slate-200 bg-slate-50 p-2 text-[11px]">
          <div className="mb-1 font-mono text-slate-600">
            {claim.call_id}
            {call ? ` · ${call.tool}` : ""} · <span className="text-emerald-700">{claim.path}</span> ={" "}
            {JSON.stringify(claim.value)}
          </div>
          {call && (
            <pre className="max-h-48 overflow-auto whitespace-pre-wrap break-all text-[10px] text-slate-700">
              {JSON.stringify(call.result, null, 1)}
            </pre>
          )}
        </div>
      )}
    </span>
  );
}
