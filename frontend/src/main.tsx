import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { BrowserRouter } from "react-router-dom";
import App from "./App";
import { BASE } from "./api";
import { AuthProvider } from "./auth";
import { applyStoredTheme } from "./shell";
import { FeedbackProvider } from "./ui";
import "./styles.css";

applyStoredTheme();

// basename keeps client routes, deep links and refreshes under the backend's base path (/moks).
createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <BrowserRouter basename={BASE || "/"}>
      <AuthProvider>
        <FeedbackProvider>
          <App />
        </FeedbackProvider>
      </AuthProvider>
    </BrowserRouter>
  </StrictMode>,
);
