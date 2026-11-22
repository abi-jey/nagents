import { createRoot } from "react-dom/client";
import { NativeWorkspace } from "./NativeWorkspace.js";
import "../app/styles.css";
import "../components/codeBlock.css";
import "../features/chat/chatMedia.css";
import "../features/chat/toolResults.css";
import "../features/chat/responseMetadata.css";
import "../features/chat/activityLayout.css";
import "../features/settings/appearance.css";
import "../features/chat/markdown.css";
import "../features/sessions/folders.css";
import "../features/sessions/sessionAgent.css";
import "../features/team/team.css";
import "../features/team/teamGroups.css";
import "../features/chat/composerTouch.css";
import "../app/panels.css";
import "./native.css";

createRoot(document.getElementById("root")!).render(<NativeWorkspace />);
