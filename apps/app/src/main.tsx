import "./index.css";

import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { createBrowserRouter } from "react-router";
import { RouterProvider } from "react-router/dom";

import { Providers } from "@/app/providers";
import { routes } from "@/app/routes";

const container = document.getElementById("root");
if (container === null) throw new Error("index.html is missing #root");

const router = createBrowserRouter(routes);

createRoot(container).render(
  <StrictMode>
    <Providers>
      <RouterProvider router={router} />
    </Providers>
  </StrictMode>,
);
