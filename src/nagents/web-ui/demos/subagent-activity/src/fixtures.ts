import type { AgentDefinition, AgentEvent } from "./types";

export const definitions: AgentDefinition[] = [
  { id: "research", parentId: "assistant", name: "Explorer", role: "Repository research", model: "gpt-6-luna", provider: "OpenAI", initials: "Ex", color: "blue", assignment: "Trace how images arrive in chat. Identify the renderer, download endpoint, and memory ownership. Report file references; do not edit." },
  { id: "builder", parentId: "assistant", name: "Builder", role: "Image experience", model: "gpt-6.1-sol", provider: "OpenAI", initials: "Bu", color: "violet", assignment: "Design inline image cards with a download action. Preserve accessible names and the original file. Ask Layout to check the mobile layout." },
  { id: "layout", parentId: "builder", name: "Layout", role: "Mobile layout review", model: "gpt-6-luna", provider: "OpenAI", initials: "La", color: "teal", assignment: "Check the image card at 390 px. Report overflow, touch-target, and focus issues to Builder. Do not change files." },
  { id: "tests", parentId: "assistant", name: "Verifier", role: "Focused regression tests", model: "gpt-6-luna", provider: "OpenAI", initials: "Ve", color: "amber", assignment: "Run image-card regressions. Report pass/fail counts and exact failures. Changes outside the test scope require approval." },
];

export const events: AgentEvent[] = [
  { id: "r1", at: 0, agentId: "research", type: "message", role: "assistant", text: "I’m tracing the attachment path from delivery to the chat renderer. I’ll send back the files that own image URLs and downloads." },
  { id: "b1", at: 0, agentId: "builder", type: "message", role: "assistant", text: "I’ll keep the image visible in the conversation and put download beside the filename. Layout is checking the narrow-screen treatment." },
  { id: "l1", at: 0, agentId: "layout", type: "message", role: "assistant", text: "The 390 px layout fits. The download button needs a 44 px touch area; the filename can wrap instead of pushing the card wider." },
  { id: "l2", at: 1, agentId: "layout", type: "status", status: "completed", activity: "Returned 2 mobile layout recommendations to Builder" },
  { id: "t1", at: 1, agentId: "tests", type: "message", role: "assistant", text: "I’ll run only the image-card tests first. This demo shows an approval checkpoint before the shell command starts." },
  { id: "r2", at: 2, agentId: "research", type: "tool_start", tool: { id: "find-images", name: "search", title: "Find image delivery and rendering paths", arguments: { query: "attachment|media_url|download", path: "src/nagents/web-ui/src", case_sensitive: false }, status: "running", summary: "Searching the chat renderer and attachment API", output: "src/features/chat/ChatMedia.tsx\nsrc/api/attachments.ts", format: "text" } },
  { id: "b2", at: 2, agentId: "builder", type: "tool_start", tool: { id: "read-image", name: "read_file", title: "Read the existing image card", arguments: { path: "src/features/chat/ChatMedia.tsx", start_line: 1, end_line: 160 }, status: "running", summary: "Reading ChatMedia.tsx · lines 1–160", output: "Image attachment renderer\nExisting controls: load, unload, download", format: "text" } },
  { id: "t2", at: 3, agentId: "tests", type: "tool_start", tool: { id: "image-tests", name: "shell", title: "Run focused image regressions", arguments: { command: "npm test -- --filter ChatMedia", cwd: "src/nagents/web-ui" }, status: "waiting", summary: "Waiting for approval to run the test command", output: "", format: "shell" } },
  { id: "t3", at: 3, agentId: "tests", type: "status", status: "waiting", activity: "Needs approval · run image-card tests" },
  { id: "r3", at: 12, agentId: "research", type: "tool_result", toolId: "find-images", status: "completed", summary: "Found 2 relevant files", output: "{\n  \"renderer\": \"src/features/chat/ChatMedia.tsx\",\n  \"delivery\": \"src/api/attachments.ts\",\n  \"download\": \"authenticated same-origin URL\"\n}" },
  { id: "r4", at: 13, agentId: "research", type: "response_boundary" },
  { id: "r5", at: 13, agentId: "research", type: "message", role: "assistant", text: "The renderer owns the object URL; the API keeps the original attachment. Download does not depend on the image staying in browser memory." },
  { id: "r6", at: 14, agentId: "research", type: "tool_start", tool: { id: "read-api", name: "read_file", title: "Verify download authorization", arguments: { path: "src/api/attachments.ts", start_line: 40, end_line: 100 }, status: "running", summary: "Checking that download stays on the ngn server", output: "Download route uses the selected chat’s attachment ID.", format: "text" } },
  { id: "b3", at: 16, agentId: "builder", type: "tool_result", toolId: "read-image", status: "completed", summary: "Read 160 lines · current controls identified", output: "Object URL cleanup happens when the card unmounts.\nThe download action already receives the original attachment." },
  { id: "b4", at: 17, agentId: "builder", type: "response_boundary" },
  { id: "b5", at: 17, agentId: "builder", type: "message", role: "assistant", text: "Layout’s review is back. I’ll use a full-width image, a wrapping filename, and a separate download button with a comfortable touch target." },
  { id: "t4", at: 19, agentId: "tests", type: "status", status: "running", activity: "Approval granted in the demo · running image-card tests" },
  { id: "b6", at: 20, agentId: "builder", type: "tool_start", tool: { id: "edit-image", name: "edit", title: "Update image-card layout", arguments: { path: "src/features/chat/ChatMedia.tsx", change: "Always render image, group filename and download action" }, status: "running", summary: "Editing image presentation and action placement", output: "− Load preview / Unload preview\n+ Inline image\n+ filename · dimensions · Download", format: "text" } },
  { id: "r7", at: 24, agentId: "research", type: "tool_result", toolId: "read-api", status: "completed", summary: "Same-origin download verified in fixture", output: "{\n  \"origin\": \"ngn server\",\n  \"authorization\": \"session-scoped\",\n  \"original_file_preserved\": true\n}" },
  { id: "r8", at: 25, agentId: "research", type: "response_boundary" },
  { id: "r9", at: 25, agentId: "research", type: "message", role: "assistant", text: "Research complete: render the image directly, retain server-backed downloads, and release object URLs when the component is removed. No provider requests are needed." },
  { id: "r10", at: 26, agentId: "research", type: "status", status: "completed", activity: "Returned renderer and download findings to Assistant" },
  { id: "t5", at: 29, agentId: "tests", type: "tool_result", toolId: "image-tests", status: "failed", summary: "1 failing test · 11 passed", output: "$ npm test -- --filter ChatMedia\n✓ renders the image inline\n✓ downloads the original attachment\n✗ download button has an accessible name\n\nExpected: /download nebula.png/i\nReceived: \"Download\"\n\n11 passed · 1 failed · 2.4 s" },
  { id: "t6", at: 30, agentId: "tests", type: "response_boundary" },
  { id: "t7", at: 30, agentId: "tests", type: "status", status: "failed", activity: "Found an accessible-name regression · report sent to Builder" },
  { id: "t8", at: 30, agentId: "tests", type: "message", role: "assistant", text: "One failure: the download button needs the filename in its accessible name. Eleven other checks passed. I’ve sent the exact failure to Builder." },
  { id: "b7", at: 33, agentId: "builder", type: "tool_result", toolId: "edit-image", status: "completed", summary: "Updated layout and accessible download name", output: "{\n  \"changed\": [\"ChatMedia.tsx\"],\n  \"image\": \"inline\",\n  \"download_label\": \"Download nebula.png\",\n  \"mobile_touch_target\": \"44 × 44 px\"\n}" },
  { id: "b8", at: 34, agentId: "builder", type: "response_boundary" },
  { id: "b9", at: 34, agentId: "builder", type: "message", role: "assistant", text: "The image is inline and the download label includes the filename. I’ve included Verifier’s accessibility fix. Ready for another focused test run." },
  { id: "b10", at: 35, agentId: "builder", type: "status", status: "completed", activity: "Returned the image-card implementation to Assistant" },
  { id: "t9", at: 37, agentId: "tests", type: "status", status: "running", activity: "Rerunning the image tests after Builder’s fix" },
  { id: "t10", at: 37, agentId: "tests", type: "tool_start", tool: { id: "retest", name: "shell", title: "Recheck the accessible download action", arguments: { command: "npm test -- --filter ChatMedia", cwd: "src/nagents/web-ui" }, status: "running", summary: "Running the same 12 focused checks", output: "✓ renders the image inline\n✓ downloads the original attachment", format: "shell" } },
  { id: "t11", at: 44, agentId: "tests", type: "tool_result", toolId: "retest", status: "completed", summary: "12 passed · 0 failed", output: "$ npm test -- --filter ChatMedia\n✓ renders the image inline\n✓ downloads the original attachment\n✓ download button has an accessible name\n\n12 passed · 0 failed · 2.1 s" },
  { id: "t12", at: 45, agentId: "tests", type: "response_boundary" },
  { id: "t13", at: 45, agentId: "tests", type: "message", role: "assistant", text: "All 12 image-card checks passed on the second run. The accessible-name failure is fixed." },
  { id: "t14", at: 46, agentId: "tests", type: "status", status: "completed", activity: "Returned 12 passing checks to Assistant" },
];

export const DEMO_END = 48;
