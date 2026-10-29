// A phone outline around the nurse view on /demo (design doc 10.3). Purely visual: the view inside
// is the same mobile-first page as /nurse/{id}.
import type { ReactNode } from "react";

export default function PhoneFrame({ children }: { children: ReactNode }) {
  return (
    <div className="relative flex h-[min(820px,calc(100vh-7.5rem))] w-[390px] max-w-full flex-col overflow-hidden rounded-[2.75rem] border-[10px] border-slate-900 bg-slate-900 shadow-2xl">
      <div className="absolute left-1/2 top-1.5 z-10 h-5 w-24 -translate-x-1/2 rounded-full bg-slate-900" />
      <div className="min-h-0 flex-1 overflow-hidden rounded-[2rem] bg-slate-50 pt-5">{children}</div>
    </div>
  );
}
