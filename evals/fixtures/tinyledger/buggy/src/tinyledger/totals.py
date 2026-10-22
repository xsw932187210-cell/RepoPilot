def net_total(amounts):
    """Return the net total of credits and refunds."""
    return sum(amount for amount in amounts if amount > 0)
