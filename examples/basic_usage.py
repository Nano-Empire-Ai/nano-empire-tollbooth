"""
nano-empire-tollbooth — 30-second demo

Run:
    pip install nano-empire-tollbooth
    python examples/basic_usage.py
"""

from nano_empire_tollbooth import monetize, get_usage


# ── Wrap any function with @monetize ────────────────────────────────────

@monetize(price_usd=0.01)
def summarize(text: str) -> str:
    """Simulate an LLM summarization call."""
    words = text.split()
    return " ".join(words[:10]) + "..." if len(words) > 10 else text


@monetize(price_usd=0.05)
def classify(text: str) -> str:
    """Simulate a classification call."""
    if "urgent" in text.lower():
        return "HIGH_PRIORITY"
    return "NORMAL"


# ── Call them like normal functions ─────────────────────────────────────

if __name__ == "__main__":
    print("=" * 50)
    print("  nano-empire-tollbooth demo")
    print("=" * 50)
    print()

    # Call summarize 3 times
    texts = [
        "The quarterly earnings report shows a significant increase in revenue across all product lines and regions",
        "Please review the attached contract and provide feedback by end of day Friday",
        "The server is experiencing intermittent timeouts during peak traffic hours and needs urgent attention",
    ]

    for i, text in enumerate(texts, 1):
        result = summarize(text)
        print(f"  summarize #{i}: {result}")

    print()

    # Call classify twice
    for text in texts[:2]:
        result = classify(text)
        print(f"  classify: {result}")

    print()

    # Show usage stats
    print("-" * 50)
    usage = get_usage()
    for fn_name, count in usage.items():
        print(f"  {fn_name}: {count} calls")
    print("-" * 50)

    print()
    print("  Every call above was metered and logged.")
    print("  Paper mode: no real charges. Full functionality.")
    print()
    print("  Upgrade to Tollbooth Pro ($19/mo) for live payments:")
    print("  https://buy.stripe.com/14A9ATaI76K8gjo9JE1Nu0h")
    print()
    print("  Powered by Nano Empire — pip install nano-empire-tollbooth")
