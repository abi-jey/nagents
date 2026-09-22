# Appointment desk

```bash
python examples/live/appointment_desk/app.py --database appointments.db --mode client
```

Open `http://localhost:3000`. Use `--mode responses` to run the same domain tools
through hosted Responses instead. Add `--store` when the OpenAI project permits
stored recordings/forks. Modes are selected at creation, never switched in flight.

This example actually saves appointments in SQLite. It offers two explicit demo
slots in August 2030, so tests and demonstrations have repeatable inventory.

1. Start a call. The assistant requests the AI/approval disclosure; the event
   timeline records acceptance separately from delivery verification.
2. Ask for availability and propose a booking, change or cancellation. The
   backend uses native tools and can optionally send progress commentary.
3. Review the exact proposal in the UI, then click **Approve current proposal**.
   Approval is an application endpoint, not a tool the model can call.
4. Ask the assistant to commit. The SQLite transaction checks the customer,
   current revision, approval, availability and operation ID. Repeating the same
   committed operation returns its saved result rather than booking twice.
5. Change the proposal while work runs. The revision advances, approval clears
   and stale results are invalidated. Approve the new proposal before committing.
6. Try typed references or an appointment-card image. Input is routed to the
   backend; the Live frontend does not receive images.
7. Start an independent report, end voice, and reconnect later. The report task
   belongs to the application, not the sideband; its persisted status/result is
   available through the backend's `get_report` tool and in the UI.
8. Inspect events, mute model input or browser playback independently, and end
   the call. A finalized stored call can be forked or downloaded from the UI.

The browser also offers verified-clip playback: first verify the recording's
wording yourself, select it, and play it with model output muted. The clip player's
`ended` event is distinct from an API acknowledgment. Playback remains muted until
you explicitly resume it; the example does not infer delivery from a transcript.

The application reuses the native WebRTC/sideband server rather than implementing
its own delegation protocol. Alternative direct SIP and G.711 media paths are
separate focused examples, since they have different media ownership.

This loopback demo represents one customer. In a deployed service, bind customer
identity and session ownership to real application authentication. SIP caller
headers and a model-supplied customer identifier are not authentication.
