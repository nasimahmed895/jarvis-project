"""
core/synonyms.py — multilingual synonym expansion for element matching.

Keyed by *intent* ("register", "submit", "email", "phone", …) so the
browser agent can ask: "find me something that means 'register'" and
try every label a real site is likely to use for that intent — in
English, Bengali, and a handful of common mixed forms.

Not a general translation system. The vocabulary is scoped to what
appears on registration / login / contact / checkout / profile forms,
which is the part `browser_control.click` and `fill_form` need help on.

Callers:
  * actions/browser_control.py:click / smart_click — expand `text`
  * actions/form_fill.py — pick the right profile field for a label
  * memory/user_profile.py — field synonym list (already does its own
    English pass; this file adds Bengali)
"""
from __future__ import annotations

from typing import Iterable


# Intent → list of surface forms. English first, Bengali second, mixed last.
_SYNONYMS: dict[str, tuple[str, ...]] = {
    # Navigation / CTA
    "register": (
        "Register", "Registration", "Sign Up", "Signup", "Sign-Up",
        "Create Account", "Create Your Account", "Create an account",
        "Join", "Join Now", "Get Started", "Start Here",
        "রেজিস্ট্রেশন", "নিবন্ধন", "একাউন্ট তৈরি করুন",
        "নতুন একাউন্ট", "নতুন একাউন্ট খুলুন", "যোগ দিন",
    ),
    "login": (
        "Login", "Log In", "Log-in", "Sign In", "Signin", "Sign-In",
        "Access Account",
        "লগইন", "লগ ইন", "সাইন ইন", "প্রবেশ করুন", "প্রবেশ",
    ),
    "logout": (
        "Logout", "Log Out", "Sign Out", "Sign-Out",
        "লগ আউট", "লগআউট", "প্রস্থান",
    ),
    "submit": (
        "Submit", "Send", "Continue", "Finish", "Complete",
        "Save", "Confirm", "Done", "OK",
        "Register Now", "Create Account", "Sign Up",
        "জমা দিন", "পাঠান", "চালিয়ে যান", "সম্পন্ন করুন",
        "একাউন্ট তৈরি করুন", "নিশ্চিত করুন", "সংরক্ষণ করুন", "ঠিক আছে",
    ),
    "next": (
        "Next", "Continue", "Proceed", "Forward",
        "পরবর্তী", "এগিয়ে যান", "চালিয়ে যান",
    ),
    "cancel": (
        "Cancel", "Dismiss", "Close", "Back",
        "বাতিল", "বাদ দিন", "বন্ধ করুন", "ফিরে যান",
    ),
    "search": (
        "Search", "Find", "Go",
        "অনুসন্ধান", "খুঁজুন", "সার্চ",
    ),

    # Form fields
    "full_name": (
        "Full Name", "Name", "Your Name", "Full-name", "Full legal name",
        "পূর্ণ নাম", "নাম", "আপনার নাম",
    ),
    "first_name": (
        "First Name", "Given Name", "First-name", "Fname",
        "প্রথম নাম", "নামের প্রথম অংশ",
    ),
    "last_name": (
        "Last Name", "Surname", "Family Name", "Lname",
        "পদবী", "পারিবারিক নাম", "নামের শেষ অংশ",
    ),
    "email": (
        "Email", "Email Address", "E-mail", "Mail",
        "ইমেইল", "ই-মেইল", "ইমেইল ঠিকানা",
    ),
    "phone": (
        "Phone", "Phone Number", "Mobile", "Mobile Number",
        "Cell", "Cell Phone", "Telephone", "Contact Number",
        "ফোন", "মোবাইল", "মোবাইল নম্বর", "ফোন নম্বর", "যোগাযোগ নম্বর",
    ),
    "password": (
        "Password", "Create Password", "Choose Password", "New Password",
        "পাসওয়ার্ড", "নতুন পাসওয়ার্ড", "পাসওয়ার্ড তৈরি করুন",
    ),
    "confirm_password": (
        "Confirm Password", "Re-enter Password", "Verify Password",
        "Password (again)",
        "পাসওয়ার্ড নিশ্চিত করুন", "পাসওয়ার্ড পুনরায় লিখুন",
    ),
    "shop_name": (
        "Shop Name", "Business Name", "Store Name", "Company",
        "দোকানের নাম", "প্রতিষ্ঠানের নাম", "ব্যবসার নাম",
    ),
    "address": (
        "Address", "Business Address", "Street Address", "Address Line 1",
        "ঠিকানা", "বাসার ঠিকানা", "ব্যবসার ঠিকানা",
    ),
    "city": (
        "City", "Town", "Locality",
        "শহর", "নগর",
    ),
    "state": (
        "State", "Province", "Region",
        "রাজ্য", "প্রদেশ",
    ),
    "postal_code": (
        "ZIP", "Zip Code", "Postal Code", "Postcode", "PIN Code",
        "পোস্টাল কোড", "পোস্ট কোড", "জিপ কোড",
    ),
    "country": (
        "Country", "Nation",
        "দেশ",
    ),
    "date_of_birth": (
        "Date of Birth", "DOB", "Birthday", "Birth Date",
        "জন্ম তারিখ", "জন্মদিন",
    ),
    "gender": (
        "Gender", "Sex",
        "লিঙ্গ",
    ),
    "username": (
        "Username", "User Name", "Login ID", "User ID", "Handle",
        "ইউজারনেম", "ব্যবহারকারীর নাম", "ইউজার আইডি",
    ),
    "otp": (
        "OTP", "One-time Password", "Verification Code",
        "ওটিপি", "যাচাইকরণ কোড", "ভেরিফিকেশন কোড",
    ),
}


def expand(intent: str) -> list[str]:
    """Return the full surface-form list for an intent."""
    key = (intent or "").lower().strip().replace("-", "_").replace(" ", "_")
    return list(_SYNONYMS.get(key, ()))


def intent_for(label: str) -> str | None:
    """Reverse lookup: given a label the page displays, return the
    canonical intent key so the caller can decide which profile field
    applies. Match is case-insensitive substring — a label like
    'Please enter your mobile number' maps to 'phone'."""
    if not label:
        return None
    needle = label.lower().strip()
    best: tuple[int, str] | None = None
    for intent, forms in _SYNONYMS.items():
        for f in forms:
            fl = f.lower()
            # Prefer longer / more specific matches. "mobile number" wins
            # over "number" when both appear.
            if fl == needle:
                return intent
            if fl in needle:
                score = len(fl)
                if best is None or score > best[0]:
                    best = (score, intent)
    return best[1] if best else None


def known_intents() -> Iterable[str]:
    return _SYNONYMS.keys()
