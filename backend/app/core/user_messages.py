"""Safe product messages for integration responses and sign-in result pages.

Preserve legacy machine-readable API codes. Add stable support codes and never
return provider exception text to a person. New error pages must use this catalog.
"""

MESSAGES = {
    "MI-MOM-001": "Meeting minutes aren’t available yet. Try again later.",
    "MI-MOM-002": "Choose a valid meeting assistance option and time zone.",
    "MI-MOM-003": "Update Meeting Intelligence before using meeting minutes in Google Meet.",
    "MI-MOM-004": "This meeting is already listening or being finished. Stop listening before finishing it here.",
    "MI-MOM-005": "We couldn’t find these meeting minutes for your account.",
    "MI-MOM-006": "We couldn’t confirm whether your recap email was sent. Check your inbox before asking for help.",
    "MI-MOM-007": "This meeting has reached the minutes recording limit. Finish it before starting another recording.",
    "MI-MOM-008": "Verify your sign-in email before enabling meeting minutes.",
    "MI-MOM-009": "We couldn’t finish clearing this meeting’s temporary content. Cleanup will be tried again.",
    "MI-MOM-010": "We couldn’t prepare your meeting recap. It can be tried again while the recording is available.",
    "MI-MOM-011": "Your meeting recap email couldn’t be sent. Check its delivery status or ask for help.",
    "MI-CON-009": "Meeting Intelligence is getting ready. Try again shortly.",
    "MI-AUTH-108": "Check your details and try again.",
    "MI-AUTH-105": "Your sign-in has expired. Sign in again to continue.",
    "MI-AUTH-104": "We couldn’t confirm your sign-in. Try again in a moment.",
    "MI-AUTH-106": "You don’t have access to this action.",
    "MI-AUTH-102": "There have been too many requests. Wait a moment, then try again.",
    "MI-AUTH-001": "Your connection needs to be renewed. Sign in again from Settings.",
    "MI-DRV-001": "We couldn’t access Google Drive. Try again from Settings.",
    "MI-DRV-002": "We couldn’t connect Google Drive. Try signing in again from Settings.",
    "MI-DRV-003": "Google Drive sign-in wasn’t completed. You can try again from Settings.",
    "MI-ATL-001": "We couldn’t access Jira and Confluence. Try again from Settings.",
    "MI-ATL-002": "We couldn’t connect Jira and Confluence. Try signing in again from Settings.",
    "MI-ATL-003": "Jira and Confluence sign-in wasn’t completed. You can try again from Settings.",
}


def product_error(code: str) -> str:
    return f"{MESSAGES[code]} (Code: {code})"
