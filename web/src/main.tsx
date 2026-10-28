import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { BrowserRouter, Navigate, Route, Routes } from "react-router-dom";
import "./index.css";
import ChargePage from "./pages/ChargePage";
import NursePage from "./pages/NursePage";

const queryClient = new QueryClient({
  defaultOptions: { queries: { staleTime: 30_000, refetchOnWindowFocus: false } },
});

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <QueryClientProvider client={queryClient}>
      <BrowserRouter>
        <Routes>
          {/* "/" goes to /demo once the demo page exists (M11) */}
          <Route path="/" element={<Navigate to="/charge" replace />} />
          <Route path="/charge" element={<ChargePage />} />
          <Route path="/nurse/:nurseId" element={<NursePage />} />
          <Route path="/nurse" element={<Navigate to="/nurse/N07" replace />} />
          <Route path="*" element={<Navigate to="/charge" replace />} />
        </Routes>
      </BrowserRouter>
    </QueryClientProvider>
  </StrictMode>,
);
