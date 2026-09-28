import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { createBrowserRouter, Navigate, RouterProvider } from "react-router-dom";
import App from "./App";
import { Backtest } from "./pages/Backtest";
import { Dashboard } from "./pages/Dashboard";
import { Explore } from "./pages/Explore";
import { Markets } from "./pages/Markets";
import { Rebalancing } from "./pages/Rebalancing";
import { Scenarios } from "./pages/Scenarios";
import { SecurityDetail } from "./pages/SecurityDetail";
import { Wealth } from "./pages/Wealth";
import "./index.css";

const queryClient = new QueryClient({
  defaultOptions: { queries: { retry: 1, refetchOnWindowFocus: false } },
});

const router = createBrowserRouter([
  {
    path: "/",
    element: <App />,
    children: [
      { index: true, element: <Navigate to="/explore" replace /> },
      { path: "explore", element: <Explore /> },
      { path: "dashboard", element: <Dashboard /> },
      { path: "markets", element: <Markets /> },
      { path: "markets/:symbol", element: <SecurityDetail /> },
      { path: "wealth", element: <Wealth /> },
      { path: "scenarios", element: <Scenarios /> },
      { path: "backtest", element: <Backtest /> },
      { path: "rebalancing", element: <Rebalancing /> },
    ],
  },
]);

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <QueryClientProvider client={queryClient}>
      <RouterProvider router={router} />
    </QueryClientProvider>
  </StrictMode>,
);
