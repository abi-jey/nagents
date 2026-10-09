type ClientActivity = {
  ready: boolean;
  operating: boolean;
  running: boolean;
  modal: boolean;
  approval: boolean;
  uploadsReady: boolean;
};

/** Keep button affordances and command admission on the same rules. */
export function availability(state: ClientActivity) {
  const navigate = state.ready && !state.operating && !state.modal;
  const free = navigate && !state.approval;
  return {
    submit: free && state.uploadsReady,
    navigate,
    create: navigate,
    settings: free && !state.running,
    channels: free,
    tools: free,
    designer: free && !state.running,
    live: free,
    trash: free,
  };
}
