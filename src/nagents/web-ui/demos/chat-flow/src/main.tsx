import React from "react";
import { createRoot } from "react-dom/client";
import { App } from "./App.js";
import "../../../src/app/styles.css";
import "../../../src/features/chat/markdown.css";
import "../../../src/features/chat/activityLayout.css";
import "../../../src/features/chat/responseMetadata.css";
import "../../../src/features/settings/appearance.css";
import "./styles.css";

createRoot(document.getElementById("root")!).render(<React.StrictMode><App /></React.StrictMode>);
