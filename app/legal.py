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

# Privacy policy (added Oct 12, 2026 for the app-store submissions — both
# stores require a linked privacy policy). Plain-English summary of what
# the app collects and does with it. Lines starting with "# " are section
# headings; the /privacy route renders them as headings, and /legal/privacy
# serves the raw text.
PRIVACY_POLICY_VERSION = "1.0"
PRIVACY_POLICY_TEXT = (
    "Rent a Biker connects riders with independent bikers. "
    "This policy explains what information we collect and what we do with it.\n"
    "\n"
    "# What we collect\n"
    "\n"
    "Account info: your name, email address, and password "
    "(passwords are stored hashed — we can't read them). "
    "Bikers also provide their city and state.\n"
    "\n"
    "# Location and ride details\n"
    "\n"
    "When you book a ride we store the pickup and drop-off addresses you "
    "enter, and we convert them to approximate coordinates so the dispatch "
    "board can rank the nearest bikers. A biker's city and state are "
    "converted the same way for matching. Addresses are used only to "
    "arrange the ride.\n"
    "\n"
    "# Photos\n"
    "\n"
    "Bikers may upload a photo of their motorcycle. Photos are stored on "
    "our server and shown to Kenneth on the review and dispatch boards, "
    "and to the rider once a biker is assigned to their ride. Photos are "
    "never sold or shared with advertisers.\n"
    "\n"
    "# Messages and agreements\n"
    "\n"
    "In-app messages between a rider and their assigned biker are stored "
    "so both parties can coordinate, and Kenneth can see them for dispatch "
    "and safety. The liability waiver and biker agreements you sign are "
    "stored along with the version of the text you signed.\n"
    "\n"
    "# Payments\n"
    "\n"
    "Completed ride fares are recorded with the exact 75/25 split (75% to "
    "the biker, 25% to Rent a Biker). Payouts are currently paid manually "
    "via PayPal, and only the payout record and an optional payment "
    "reference are stored — we never see or store your full card or bank "
    "account details.\n"
    "\n"
    "# What we share — and what we never do\n"
    "\n"
    "When a ride is matched, the rider and the assigned biker see each "
    "other's name and contact information so they can coordinate the ride. "
    "Kenneth, the operator, can see all records to run dispatch. We never "
    "sell your personal information, and we don't share it with "
    "advertisers. We only disclose records when required by law.\n"
    "\n"
    "# How long we keep it\n"
    "\n"
    "We keep your records while your account exists and as long as needed "
    "to run the business and settle payouts. You can ask us to delete your "
    "account and data at any time.\n"
    "\n"
    "# Your choices\n"
    "\n"
    "You can review and update your profile in the app. To get a copy of "
    "your data or ask for deletion, email us — see Contact below.\n"
    "\n"
    "# Contact\n"
    "\n"
    "Questions about this policy: rentabikerservice@gmail.com."
)

ALL_TEXTS = {
    "rider-waiver": (RIDER_WAIVER_VERSION, RIDER_WAIVER_TEXT),
    "biker-pledge": (BIKER_PLEDGE_VERSION, BIKER_PLEDGE_TEXT),
    "biker-liability": (BIKER_LIABILITY_VERSION, BIKER_LIABILITY_TEXT),
    "passenger-responsibility": (
        RESPONSIBILITY_AGREEMENT_VERSION,
        RESPONSIBILITY_AGREEMENT_TEXT,
    ),
    "privacy-policy": (PRIVACY_POLICY_VERSION, PRIVACY_POLICY_TEXT),
}
