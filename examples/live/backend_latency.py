"""Tune supported backend settings; compare useful results and task success, not just preambles."""

from _support import drive
from _support import execute
from _support import parser
from _support import voice

from nagents.live import LiveConfig


async def main() -> None:
    cli = parser(__doc__ or "Backend latency")
    cli.add_argument("--model", default="gpt-5.6-luna")
    cli.add_argument("--tier", choices=("auto", "default", "priority", "flex"), default="auto")
    args = cli.parse_args()
    config = LiveConfig(
        backend_model=args.model,
        web_search=True,
        backend_options={"service_tier": args.tier, "reasoning": {"effort": "low"}, "text": {"verbosity": "low"}},
    )
    await drive(voice(config), duration=args.duration)


if __name__ == "__main__":
    execute(main)
