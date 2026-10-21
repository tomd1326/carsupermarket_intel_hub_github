"""The password rules, in one place. The set-password page, the Add / Edit user
popup and the prod save all check a new password with password_error().

The checklist on templates/set_password.html repeats the same four tests in
JavaScript so it can tick as the user types; keep the two in step. The server
check is the one that counts."""

import re

PASSWORD_MIN_LENGTH = 6

# (key, label) - labels are what the user sees on the checklist and in errors.
PASSWORD_RULES = (
    ('length', f"At least {PASSWORD_MIN_LENGTH} characters"),
    ('number', "A number"),
    ('upper', "An upper case letter"),
    ('special', "A special character"),
)

PASSWORD_RULES_TEXT = ", ".join(label[0].lower() + label[1:] for _, label in PASSWORD_RULES)


def failed_rules(password: str) -> list:
    """Keys of the rules this password breaks, in checklist order."""
    tests = {
        'length': len(password) >= PASSWORD_MIN_LENGTH,
        'number': re.search(r'\d', password) is not None,
        'upper': re.search(r'[A-Z]', password) is not None,
        'special': re.search(r'[^A-Za-z0-9]', password) is not None,
    }
    return [key for key, _ in PASSWORD_RULES if not tests[key]]


def password_error(password: str) -> str | None:
    """None when the password is fine, otherwise a one-line message."""
    failed = failed_rules(password)
    if not failed:
        return None
    labels = dict(PASSWORD_RULES)
    return "Password needs: " + ", ".join(labels[k][0].lower() + labels[k][1:] for k in failed)
