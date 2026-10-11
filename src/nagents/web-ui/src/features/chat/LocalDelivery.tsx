import { createContext, useContext } from "react";
import { ChatMedia } from "./ChatMedia.js";
import { MEDIA_TYPES, type LocalDelivery } from "./deliveries.js";
import { MessageContent } from "./MessageContent.js";

export const MediaToken = createContext("");

export function LocalDeliveryCard({ delivery }: { delivery: LocalDelivery }) {
  const token = useContext(MediaToken);
  return <section className="local-delivery" aria-label="Shared files" data-delivery-id={delivery.delivery_id}>
    {delivery.earlier && <p className="record-note">From earlier in this conversation</p>}
    {delivery.text && <MessageContent text={delivery.text} />}
    <div className={`chat-media-gallery${delivery.assets.length > 1 ? " has-many" : ""}`}>
      {delivery.assets.map(asset => {
        const path = ["sessions", delivery.session_id, "deliveries", delivery.delivery_id, "assets", asset.asset_id].map(encodeURIComponent).join("/");
        return MEDIA_TYPES.has(asset.media_type)
          ? <ChatMedia key={JSON.stringify([path, asset.media_type, asset.byte_length])} token={token} path={path} asset={asset} />
          : <p key={asset.asset_id} className="record-note">{asset.filename} · This file type cannot be displayed.</p>;
      })}
    </div>
  </section>;
}
