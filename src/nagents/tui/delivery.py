"""Text-only presentation of durable explicit deliveries, including historic media."""

from textual.app import ComposeResult
from textual.containers import Vertical
from textual.widgets import Static

from nagents.channels.delivery_types import DeliveryReceipt


def safe_text(text: str) -> str:
    """No terminal controls or markup interpretation in transport metadata."""
    return "".join(char for char in text if char.isprintable() or char in "\n\t")


class DeliveryWidget(Vertical):
    def __init__(self, receipt: DeliveryReceipt) -> None:
        super().__init__(classes="channel-delivery")
        self.receipt = receipt

    def compose(self) -> ComposeResult:
        yield Static(f"DELIVERY / {safe_text(self.receipt.channel)}", markup=False, classes="delivery-label")
        if self.receipt.text:
            yield Static(safe_text(self.receipt.text), markup=False)
        for asset in self.receipt.assets:
            yield Static(
                f"Attachment: {safe_text(asset.filename)} ({safe_text(asset.media_type)}) "
                f"via {safe_text(self.receipt.channel)} — not rendered in the terminal",
                markup=False,
                classes="delivery-attachment",
            )
