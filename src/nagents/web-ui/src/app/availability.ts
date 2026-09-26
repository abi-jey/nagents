type ClientActivity = {
  ready: boolean;
  operating: boolean;
  running: boolean;
  dictating: boolean;
  reviewing: boolean;
  modal: boolean;
  approval: boolean;
  uploadsReady: boolean;
};

/** Keep button affordances and command admission on the same rules. */
export function availability(state: ClientActivity) {
  const free = state.ready && !state.operating && !state.modal && !state.approval;
  const navigate = free && !state.dictating;
  return {
    submit: navigate && state.uploadsReady,
    navigate,
    create: navigate && !state.running,
    settings: navigate && !state.running,
    channels: navigate,
    tools: navigate,
    designer: navigate && !state.running,
    trash: free && (!state.dictating || state.reviewing),
  };
}
