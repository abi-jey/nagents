import { createRoot } from "react-dom/client";
import { App } from "./app/App";
import "./app/styles.css";
import "./features/chat/chatMedia.css";
import "./features/chat/toolResults.css";
import "./features/chat/responseMetadata.css";
import "./features/sessions/folders.css";
import "./features/tools/tools.css";
import "./features/context/context.css";
import "./features/sessions/sessionAgent.css";

createRoot(document.getElementById("root")!).render(<App />);
