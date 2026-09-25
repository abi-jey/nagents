# Channel API

Channels are independently installable transports for one persistent agent
identity. See [Channels and Listening](../guide/channels.md) for lifecycle,
delivery semantics, and an extension example.

::: nagents.channels.Channel

::: nagents.channels.ChannelDispatcher

::: nagents.channels.ChannelContentCapabilities

::: nagents.channels.ChannelReceiveCapabilities

::: nagents.channels.ChannelSendCapabilities

::: nagents.channels.ChannelRenderCapabilities

::: nagents.channels.ChannelMessage

::: nagents.channels.ChannelSend

::: nagents.channels.ChannelAttachment

::: nagents.channels.ChannelDelivery

::: nagents.channels.ChannelAction

::: nagents.channels.ChannelPlugin

::: nagents.channels.ChannelCommand

::: nagents.channels.ChannelActivity

::: nagents.channels.ChannelError

::: nagents.channels.ChannelEvent

::: nagents.channels.load_channel

## Local delivery metadata

These records describe durable local deliveries separately from model messages.
An origin is correlation data, not authorization; host execution must supply and
validate it. Tool receipts and notifications contain references rather than media
bytes. Concrete interface adapters use the journal through their owned host.

::: nagents.channels.delivery_types.DeliveryOrigin

::: nagents.channels.delivery_types.DeliveryAsset

::: nagents.channels.delivery_types.DeliveryReceipt

::: nagents.channels.delivery_types.DeliveryHistory

::: nagents.channels.local_delivery.LocalDeliveryService

::: nagents.session.deliveries.DeliveryJournal

::: nagents.session.deliveries.PreparedDelivery
