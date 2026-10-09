import { createRoot } from "react-dom/client";
import { App } from "./app/App";
import "./app/styles.css";
import "./features/sessions/folders.css";

createRoot(document.getElementById("root")!).render(<App />);
