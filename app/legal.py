"""Versioned legal texts: the rider liability waiver, the trusted-biker
pledge, and the biker liability agreement.

The app serves the current text from the server (GET /legal/*) so the
frontend never hard-codes it, and every booking / biker application records
the exact text version the user signed — so if the wording ever changes,
the signed record still points at what was actually agreed to.
"""

# Rider liability waiver — same text that has appeared in the booking form
# since milestone 3 (Oct 7, 2026). Rent a Biker is a connection business:
# bikers are independent riders, not employees or agents, and the business
# is not liable for the ride itself.
RIDER_WAIVER_VERSION = "1.0"
RIDER_WAIVER_TEXT = (
    "I understand Rent a Biker is a connection service: bikers are "
    "independent riders, not employees or agents, and Rent a Biker is not "
    "liable for the ride itself. I ride at my own risk and release "
    "Rent a Biker from claims arising from the ride."
)

# Trusted-biker pledge, accepted on every biker application.
BIKER_PLEDGE_VERSION = "1.0"
BIKER_PLEDGE_TEXT = (
    "Trusted-biker pledge — I agree to ride safe, follow all traffic laws, "
    "and treat every rider with respect."
)

# Biker liability agreement, accepted on every biker application. The biker
# is an independent rider: Rent a Biker connects riders and bikers and is
# not a party to the ride, not an employer or agent of the biker.
BIKER_LIABILITY_VERSION = "1.0"
BIKER_LIABILITY_TEXT = (
    "I understand Rent a Biker is a connection service only: I am an "
    "independent rider, not an employee or agent of Rent a Biker, and "
    "Rent a Biker is not liable for any ride I take through the app. I "
    "accept personal responsibility for my conduct on the ride."
)

# Passenger-responsibility agreement (Kenneth's insurance rule, Oct 7, 2026):
# required when a biker's insurance policy does NOT cover passengers.
RESPONSIBILITY_AGREEMENT_VERSION = "1.0"
RESPONSIBILITY_AGREEMENT_TEXT = (
    "My motorcycle insurance does not cover passengers, so I accept "
    "personal responsibility for anything that happens to the passenger "
    "during the ride."
)

ALL_TEXTS = {
    "rider-waiver": (RIDER_WAIVER_VERSION, RIDER_WAIVER_TEXT),
    "biker-pledge": (BIKER_PLEDGE_VERSION, BIKER_PLEDGE_TEXT),
    "biker-liability": (BIKER_LIABILITY_VERSION, BIKER_LIABILITY_TEXT),
    "passenger-responsibility": (
        RESPONSIBILITY_AGREEMENT_VERSION,
        RESPONSIBILITY_AGREEMENT_TEXT,
    ),
}
