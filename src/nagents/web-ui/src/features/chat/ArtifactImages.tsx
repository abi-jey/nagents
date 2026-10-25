import { useContext } from "react";
import type { ArtifactImageRef } from "../../types.js";
import { ChatMedia } from "./ChatMedia.js";
import { MediaToken } from "./LocalDelivery.js";
import { artifactState } from "./artifactImages.js";

export function ArtifactImages({ artifacts, error }: { artifacts?: ArtifactImageRef[]; error?: string }) {
  const token = useContext(MediaToken), checked = artifactState(artifacts);
  const images = checked.artifacts || [], problem = error || checked.artifactError;
  if (!images.length && !problem) return null;
  return <section className="artifact-images" aria-label="Image artifacts">
    {problem && <p className="artifact-image-error record-note" role="status">{problem}</p>}
    {!!images.length && <div className={`chat-media-gallery${images.length > 1 ? " has-many" : ""}`}>
      {images.map(image => <ChatMedia key={image.path} token={token} path={image.path.slice(5)} asset={image} caption={image.caption} />)}
    </div>}
  </section>;
}
