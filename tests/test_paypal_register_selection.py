from modules.paypal_register import filter_accounts_by_email
from modules.storage import MailAccount


def test_filter_accounts_by_email_is_case_insensitive() -> None:
    accounts = [
        MailAccount(email="first@hotmail.com", raw="first@hotmail.com----x"),
        MailAccount(email="Target@Hotmail.com", raw="Target@Hotmail.com----x"),
    ]

    selected = filter_accounts_by_email(accounts, "target@hotmail.com")

    assert [account.email for account in selected] == ["Target@Hotmail.com"]


def test_filter_accounts_by_email_returns_all_when_empty() -> None:
    accounts = [MailAccount(email="first@hotmail.com", raw="first@hotmail.com----x")]

    assert filter_accounts_by_email(accounts, "") == accounts
