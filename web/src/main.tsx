import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { BrowserRouter, Navigate, Route, Routes } from "react-router-dom";
import { ApiError } from "./api/client";
import "./index.css";
import ChargePage from "./pages/ChargePage";
import DemoPage from "./pages/DemoPage";
import NursePage from "./pages/NursePage";

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      staleTime: 30_000,
      refetchOnWindowFocus: false,
      // 4xx (ApiError carries the status) never retries; anything else retries twice
      retry: (n, err) => !(err instanceof ApiError && err.status >= 400 && err.status < 500) && n < 2,
    },
  },
});

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <QueryClientProvider client={queryClient}>
      <BrowserRouter>
        <Routes>
          <Route path="/" element={<Navigate to="/demo" replace />} />
          <Route path="/demo" element={<DemoPage />} />
          <Route path="/charge" element={<ChargePage />} />
          <Route path="/nurse/:nurseId" element={<NursePage />} />
          <Route path="/nurse" element={<Navigate to="/nurse/N07" replace />} />
          <Route path="*" element={<Navigate to="/demo" replace />} />
        </Routes>
      </BrowserRouter>
    </QueryClientProvider>
  </StrictMode>,
);
