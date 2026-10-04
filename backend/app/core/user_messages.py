"""Safe product messages for integration responses and sign-in result pages.

Preserve legacy machine-readable API codes. Add stable support codes and never
return provider exception text to a person. New error pages must use this catalog.
"""

MESSAGES = {
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
