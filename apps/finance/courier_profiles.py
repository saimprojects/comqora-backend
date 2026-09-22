"""Courier statement vocabulary.

The parser deliberately uses *labels*, not page coordinates.  Courier portals
change their layout often, while the financial meaning of headings tends to be
stable.  A profile can make a known label safer without making an unknown
deduction silently look like a shipping expense.
"""

import re


def normalize_label(value):
    """Return a comparison key for portal headings and courier names."""
    return re.sub(r"[^a-z0-9]+", " ", str(value or "").lower()).strip()


# These are deliberately broad name aliases.  A selected workspace courier is
# still authoritative; source text merely supplies useful evidence for review.
COURIER_PROFILES = {
    "postex": {
        "names": ("postex", "post ex"),
        # PostEx's CPR page uses these labels.  GST is a charge on the courier
        # service; the separate 4% deduction remains a non-cost withholding.
        "roles": {
            "sr no": "info",
            "order ref": "order_ref",
            "tracking number": "tracking",
            "weight kg": "info",
            "pickup date": "info",
            "origin city": "info",
            "delivery city": "info",
            "status": "info",
            # COD is the customer-facing order value.  PostEx also prints it on
            # returns, where no cash is payable.  Reserve Amount is the actual
            # settled/held amount and therefore the safer payout gross column.
            "cod amount": "info",
            "upfront amount": "info",
            "reserve amount": "gross",
            "dr date": "info",
            "shipping charges": "fee",
            "upfront charges": "fee",
            "gst": "fee",
            "deduction 4": "deduction",
            "net amount": "net",
        },
        "markers": ("reserve amount", "upfront charges", "deduction 4", "cpr no"),
        "reference_terms": ("cpr", "cpr no", "cpr number"),
        "net_terms": ("net total", "payable amount", "net amount"),
    },
    "tcs": {
        "names": ("tcs", "tcs express"),
        "roles": {
            "booking date": "info",
            "cn by courier": "tracking",
            "cn status": "info",
            "order no": "order_ref",
            "payment status": "info",
            "amount paid": "gross",
            "parcel weight": "info",
            "city": "info",
            "delivery charges": "fee",
            "delivery date": "info",
            "payment date": "info",
        },
        "markers": ("cn by courier", "payment status", "amount paid"),
        "reference_terms": ("payment reference", "remittance", "settlement"),
        "net_terms": ("net payable", "payable amount", "remittance amount"),
    },
    "leopards": {
        "names": ("leopards", "leopard", "lcs", "leopards courier"),
        "roles": {},
        "reference_terms": ("remittance", "payment reference", "settlement"),
        "net_terms": ("net payable", "payable amount", "net amount"),
    },
    "mnp": {
        "names": ("m p", "mnp", "m p express", "m p courier", "m and p"),
        "roles": {},
        "reference_terms": ("remittance", "settlement", "payment reference"),
        "net_terms": ("net payable", "payable amount", "net amount"),
    },
    "trax": {
        "names": ("trax", "trax logistics", "call courier", "callcourier"),
        "roles": {},
        "reference_terms": ("remittance", "settlement", "payment reference"),
        "net_terms": ("net payable", "payable amount", "net amount"),
    },
    "blueex": {
        "names": ("blueex", "blue ex", "blueex courier", "blue ex courier"),
        "roles": {
            "cnno": "tracking",
            "cn no": "tracking",
            "reference": "order_ref",
            "amount": "info",
            "amount received": "gross",
            "blue ex charges": "fee",
            "blueex charges": "fee",
            "statement period": "info",
            "generation date time": "info",
            "customer account name": "info",
            "status": "info",
            "cod amount": "info",
        },
        "markers": ("cnno", "blue ex charges", "detail of cod shipment"),
        "reference_terms": ("remittance", "settlement", "payment reference"),
        "net_terms": ("net payable", "payable amount", "net amount"),
    },
    "rider": {
        "names": ("rider", "rider pakistan"),
        "roles": {},
        "reference_terms": ("remittance", "settlement", "payment reference"),
        "net_terms": ("net payable", "payable amount", "net amount"),
    },
    "swyft": {
        "names": ("swyft", "swyft logistics"),
        "roles": {},
        "reference_terms": ("remittance", "settlement", "payment reference"),
        "net_terms": ("net payable", "payable amount", "net amount"),
    },
    "forrun": {
        "names": ("forrun", "for run"),
        "roles": {},
        "reference_terms": ("remittance", "settlement", "payment reference"),
        "net_terms": ("net payable", "payable amount", "net amount"),
    },
    "runcourier": {
        "names": ("run courier", "runcourier", "run"),
        "roles": {},
        "reference_terms": ("remittance", "settlement", "payment reference"),
        "net_terms": ("net payable", "payable amount", "net amount"),
    },
    "sonic": {
        "names": ("sonic", "sonic courier"),
        "roles": {},
        "reference_terms": ("remittance", "settlement", "payment reference"),
        "net_terms": ("net payable", "payable amount", "net amount"),
    },
}


def profile_for(courier_hint="", source_text=""):
    """Find a named profile from strong template evidence, then courier identity."""
    selected = normalize_label(courier_hint)
    source = normalize_label(source_text[:12000])
    # Strong template markers beat an ambiguous courier name. For example,
    # current CPRs can mention Call Courier while using the PostEx CPR schema.
    marker_matches = [
        (key, profile, sum(marker in source for marker in profile.get("markers", ())))
        for key, profile in COURIER_PROFILES.items()
    ]
    marker_matches = [match for match in marker_matches if match[2] >= 2]
    if marker_matches:
        key, profile, _ = max(marker_matches, key=lambda match: match[2])
        return key, profile, "document template markers"
    for key, profile in COURIER_PROFILES.items():
        names = tuple(normalize_label(name) for name in profile["names"])
        if selected and any(name and (name == selected or name in selected) for name in names):
            return key, profile, "selected courier"
        if any(name and re.search(rf"(?:^| ){re.escape(name)}(?: |$)", source) for name in names):
            return key, profile, "document text"
    return "generic", {"roles": {}, "reference_terms": (), "net_terms": ()}, "generic labels"


def role_override(label, profile):
    """Read a profile mapping after normalising a multi-line portal heading."""
    key = normalize_label(label)
    if key in profile.get("roles", {}):
        return profile["roles"][key]
    return None
