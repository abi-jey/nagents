import { createRoot } from "react-dom/client";
import { App } from "./app/App";
import "./app/styles.css";
import "./features/chat/chatMedia.css";
import "./features/chat/toolResults.css";
import "./features/sessions/folders.css";
import "./features/tools/tools.css";

createRoot(document.getElementById("root")!).render(<App />);
